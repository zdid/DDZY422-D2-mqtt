#!/usr/bin/env python3
# DDZY422-D2 (compteur monophasé à module Wi-Fi Solarman intégré, RS485 / Modbus RTU) -> MQTT, avec découverte
# automatique Home Assistant.
#
# Sans dépendance : python3 (termios de la bibliothèque standard) et `mosquitto_pub` (paquet mosquitto-clients).
#
# Le module Wi-Fi du compteur interroge lui-même le compteur sur le MÊME bus RS485 (une lecture des 34 registres par
# minute environ, plus un « battement » toutes les 30 s). Ce script s'intercale entre ces échanges : il écoute le
# bus, repère la lecture périodique du module, et n'émet jamais pendant ses échanges. S'il n'y a pas de module Wi-Fi
# (ou si vous l'avez retiré), il interroge librement (`--module-wifi non`).
#
# Registres (adresse 1, 9600 bauds, 8E1, fonction 3, lecture de 34 registres depuis 0x0000) — déduits de l'écoute
# passive du bus et comparés à un DDSU666-H sur la même ligne (voir le README) :
#   0x0001 tension (×0,1 V) · 0x0002-03 courant (×0,001 A) · 0x0006-07 puissance active (W) · 0x0008-09 réactive (var)
#   0x000B apparente (VA) · 0x000C facteur de puissance (bit 15 = signe, ×0,001) · 0x000D fréquence (×0,01 Hz)
#   0x000E-0F énergie importée (×0,01 kWh) · 0x0018-19 énergie exportée (×0,01 kWh)
#   Signe : un premier registre à 0x8000 devant la valeur = valeur négative (puissance négative = injection).
#
# LECTURE SEULE : une seule requête, fonction 3. Le script n'écrit JAMAIS dans le compteur (le module Wi-Fi, lui,
# écrit un registre de service toutes les 30 s : ne pas l'imiter).
#
# Home Assistant : messages de découverte RETENUS sous <préfixe>/sensor/<nœud>/<mesure>/config (un appareil, 10 capteurs),
# état en un seul message JSON sur <thème>/state, `expire_after` : si le script s'arrête, les capteurs passent « indisponible ».
#
# Réglages : options de la ligne de commande ET/OU fichier de configuration (--config fichier.conf, format INI, voir
# ddzy422d2-mqtt.conf.example). Priorité : ligne de commande > fichier > valeurs par défaut.
#
# Usage : python3 ddzy422d2-mqtt.py [options]      (--help pour la liste ; --dry-run n'envoie rien à MQTT)

import argparse
import configparser
import json
import os
import select
import struct
import subprocess
import sys
import termios
import time

BAUDS = {2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400}

NREG = 34                      # registres lus à chaque interrogation (0x0000 à 0x0021), comme le module Wi-Fi
PERIOD = 61.0                  # cycle du module Wi-Fi (s), compté à partir de sa lecture des 34 registres (T0)
FORBIDDEN = ((-1.5, 2.0), (21.5, 25.0), (52.5, 56.0))   # fenêtres du module après T0 : lecture, 2 battements (s)
MODULE_STALE = 150.0           # au-delà, le module est considéré absent tant qu'on ne le revoit pas
MODULE_WAIT = 90.0             # attente maximale du premier échange du module au démarrage (mode auto)
SILENCE = 0.15                 # silence du bus exigé avant d'émettre (s)


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def open_serial(dev, baud, parity):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
    cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.PARODD | termios.CSTOPB | termios.CRTSCTS)) | termios.CS8 | termios.CREAD | termios.CLOCAL
    if parity == "E":
        cflag |= termios.PARENB
    termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, BAUDS[baud], BAUDS[baud], cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def build_request(addr):
    body = struct.pack(">BBHH", addr, 3, 0x0000, NREG)
    return body + struct.pack("<H", crc16_modbus(body))


class Bus:
    """Écoute du bus : repère la lecture périodique du module Wi-Fi (sa requête = la nôtre, mêmes octets)."""

    def __init__(self, fd, request):
        self.fd, self.request = fd, request
        self.buf = b""
        self.last_rx = 0.0
        self.t0 = None          # heure de la dernière lecture du module vue sur le fil

    def listen(self, duration):
        end = time.time() + duration
        while True:
            left = end - time.time()
            if left <= 0:
                return
            ready, _, _ = select.select([self.fd], [], [], min(left, 0.05))
            if ready:
                try:
                    data = os.read(self.fd, 256)
                except BlockingIOError:
                    continue
                if data:
                    self.last_rx = time.time()
                    self.buf = (self.buf + data)[-64:]
                    if self.buf.endswith(self.request):
                        self.t0 = self.last_rx

    def module_busy(self, mode, now):
        """Vrai si `now` tombe dans une fenêtre d'échange du module Wi-Fi (jamais en mode « non »)."""
        if mode == "non" or self.t0 is None or now - self.t0 > MODULE_STALE:
            return False
        phase = (now - self.t0) % PERIOD
        if phase > PERIOD - 2.0:
            phase -= PERIOD
        return any(a <= phase <= b for a, b in FORBIDDEN)


def poll(bus, addr, timeout=0.6):
    """Dictionnaire des mesures, ou None si pas de réponse valide."""
    fd = bus.fd
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, bus.request)
    need = 5 + 2 * NREG
    head = bytes([addr, 3, 2 * NREG])
    raw = b""
    end = time.time() + timeout
    start = -1
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            raw += os.read(fd, 256)
        start = raw.find(head)          # saute l'écho éventuel de notre requête
        if start >= 0 and len(raw) >= start + need:
            break
    bus.buf, bus.last_rx = b"", time.time()
    if start < 0 or len(raw) < start + need:
        return None
    frame = raw[start:start + need]
    if crc16_modbus(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
        return None
    w = [(frame[3 + 2 * i] << 8) | frame[4 + 2 * i] for i in range(NREG)]
    signed = lambda i, scale: (-1 if w[i] == 0x8000 else 1) * w[i + 1] * scale   # registre i = signe, i+1 = valeur
    energy = lambda i: ((w[i] << 16) | w[i + 1]) * 0.01
    pf = (w[0x0C] & 0x7FFF) * 0.001 * (-1 if w[0x0C] & 0x8000 else 1)
    return {
        "voltage": round(w[1] * 0.1, 1),
        "current": round(w[3] * 0.001, 3),
        "power": round(signed(6, 1), 1),
        "reactive_power": round(signed(8, 1), 1),
        "apparent_power": float(w[0x0B]),
        "power_factor": round(pf, 3),
        "frequency": round(w[0x0D] * 0.01, 2),
        "energy_total": round(energy(0x0E) - energy(0x18), 2),
        "energy_import": round(energy(0x0E), 2),
        "energy_export": round(energy(0x18), 2),
    }


# clé -> (nom, device_class, unité, state_class, précision)
SENSORS = {
    "voltage": ("Tension", "voltage", "V", "measurement", 1),
    "current": ("Courant", "current", "A", "measurement", 2),
    "power": ("Puissance active (positive = soutirage)", "power", "W", "measurement", 0),
    "reactive_power": ("Puissance réactive", "reactive_power", "var", "measurement", 0),
    "apparent_power": ("Puissance apparente", "apparent_power", "VA", "measurement", 0),
    "power_factor": ("Facteur de puissance", "power_factor", None, "measurement", 3),
    "frequency": ("Fréquence", "frequency", "Hz", "measurement", 2),
    "energy_total": ("Énergie totale (import - export)", "energy", "kWh", "total", 2),
    "energy_import": ("Énergie importée", "energy", "kWh", "total_increasing", 2),
    "energy_export": ("Énergie exportée", "energy", "kWh", "total_increasing", 2),
}


def discovery_messages(args):
    device = {"identifiers": [args.node], "name": args.name, "model": "DDZY422-D2"}
    messages = []
    for key, (label, dclass, unit, sclass, precision) in SENSORS.items():
        payload = {
            "name": label,
            "unique_id": "%s_%s" % (args.node, key),
            "object_id": "%s_%s" % (args.node, key),
            "state_topic": args.topic + "/state",
            "value_template": "{{ value_json.%s }}" % key,
            "device_class": dclass,
            "state_class": sclass,
            "suggested_display_precision": precision,
            "expire_after": max(60, args.interval * 6),
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        messages.append(("%s/sensor/%s/%s/config" % (args.prefix, args.node, key), json.dumps(payload, ensure_ascii=False), True))
    return messages


def publish(args, topic, payload, retain=False):
    if args.dry_run:
        print("MQTT %s%s %s" % (topic, " (retenu)" if retain else "", payload))
        return True
    cmd = ["mosquitto_pub", "-h", args.mqtt_host, "-p", str(args.mqtt_port), "-t", topic, "-m", payload, "-q", "0"]
    if args.mqtt_user:
        cmd += ["-u", args.mqtt_user]
        if args.mqtt_password:
            cmd += ["-P", args.mqtt_password]
    if retain:
        cmd.append("-r")
    try:
        return subprocess.run(cmd, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


BOOLEANS = ("once", "dry_run")


def load_config(path, valid):
    """Lit le fichier INI (section [ddzy422d2]) ; renvoie {nom_d_option: valeur}. Les clés sont les noms des options
    sans « -- » (tiret ou souligné indifféremment : mqtt-host = mqtt_host)."""
    cp = configparser.ConfigParser(interpolation=None)   # sans interpolation : un mot de passe peut contenir « % »
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    if not cp.has_section("ddzy422d2"):
        sys.exit("%s : section [ddzy422d2] absente" % path)
    values = {}
    for key, raw in cp.items("ddzy422d2"):
        dest = key.strip().lower().replace("-", "_")
        if dest not in valid:
            sys.exit("%s : option inconnue « %s »" % (path, key))
        values[dest] = raw.strip().lower() in ("1", "true", "yes", "oui", "on") if dest in BOOLEANS else raw.strip()
    return values


def main():
    ap = argparse.ArgumentParser(description="DDZY422-D2 (RS485) -> MQTT + découverte Home Assistant")
    ap.add_argument("--config", help="fichier de configuration INI (section [ddzy422d2]) ; la ligne de commande a la priorité")
    ap.add_argument("--device", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="E", choices=["N", "E"])
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default="", help="nom d'utilisateur MQTT (vide = connexion anonyme)")
    ap.add_argument("--mqtt-password", default="", help="mot de passe MQTT (préférer le fichier de configuration, voir le README)")
    ap.add_argument("--prefix", default="homeassistant", help="préfixe de découverte de Home Assistant")
    ap.add_argument("--topic", default="ddzy422d2", help="thème de l'état")
    ap.add_argument("--node", default="ddzy422d2")
    ap.add_argument("--name", default="Compteur DDZY422-D2")
    ap.add_argument("--interval", type=int, default=5, help="secondes entre deux lectures (1 à 60)")
    ap.add_argument("--module-wifi", default="auto", choices=["auto", "non"],
                    help="auto : s'intercale entre les échanges du module Wi-Fi s'il est présent ; non : interroge librement (pas de module)")
    ap.add_argument("--once", action="store_true", help="une seule lecture puis sortie")
    ap.add_argument("--dry-run", action="store_true", help="affiche les messages au lieu de les envoyer à MQTT")
    known, _ = ap.parse_known_args()
    if known.config:
        try:
            values = load_config(known.config, {a.dest for a in ap._actions} - {"help", "config"})
        except OSError as e:
            sys.exit("fichier de configuration illisible : %s" % e)
        ap.set_defaults(**values)
        if values.get("mqtt_password") and os.stat(known.config).st_mode & 0o077:
            print("Attention : %s contient un mot de passe et est lisible par d'autres utilisateurs (chmod 600)" % known.config,
                  file=sys.stderr, flush=True)
    args = ap.parse_args()
    if args.baud not in BAUDS:
        ap.error("--baud : valeurs possibles %s" % sorted(BAUDS))
    if args.parity not in ("N", "E"):
        ap.error("--parity : N ou E")
    if args.module_wifi not in ("auto", "non"):
        ap.error("--module-wifi : auto ou non")
    if args.mqtt_password and not args.mqtt_user:
        ap.error("--mqtt-password demande --mqtt-user")
    args.interval = min(60, max(1, args.interval))

    fd = open_serial(args.device, args.baud, args.parity)
    bus = Bus(fd, build_request(args.address))
    print("DDZY422-D2 adresse %d sur %s -> MQTT %s:%d (%s), toutes les %d s, module Wi-Fi : %s"
          % (args.address, args.device, args.mqtt_host, args.mqtt_port, args.topic, args.interval, args.module_wifi), flush=True)

    if args.module_wifi == "auto":
        bus.listen(0.0)
        end = time.time() + MODULE_WAIT
        while bus.t0 is None and time.time() < end:
            bus.listen(0.5)
        print("module Wi-Fi détecté : lectures intercalées entre ses échanges" if bus.t0 else
              "aucun échange de module Wi-Fi vu en %d s : interrogation libre (il sera pris en compte s'il apparaît)" % MODULE_WAIT, flush=True)

    last_discovery, failures = 0.0, 0
    next_read = time.time()
    while True:
        bus.listen(max(0.0, next_read - time.time()))
        if bus.module_busy(args.module_wifi, time.time()):
            next_read += args.interval                       # créneau pris par le module : on saute celui-ci
            continue
        while time.time() - bus.last_rx < SILENCE:
            bus.listen(0.05)
        if bus.module_busy(args.module_wifi, time.time()):
            next_read += args.interval
            continue
        started = time.time()
        if started - last_discovery > 600:                   # découverte retenue, republiée toutes les 10 min
            for topic, payload, retain in discovery_messages(args):
                publish(args, topic, payload, retain)
            last_discovery = started
        values = poll(bus, args.address)
        if values is None:
            failures += 1
            if failures in (1, 5) or failures % 60 == 0:
                print("lecture impossible (%d échec(s) de suite) — câblage, adresse, parité ou port occupé ?" % failures, flush=True)
        else:
            if failures:
                print("lecture rétablie après %d échec(s)" % failures, flush=True)
            failures = 0
            publish(args, args.topic + "/state", json.dumps(values), False)
        if args.once:
            sys.exit(0 if values else 1)
        next_read += args.interval
        if next_read < time.time():                           # en retard : pas de rafale de rattrapage
            next_read = time.time() + args.interval


if __name__ == "__main__":
    main()

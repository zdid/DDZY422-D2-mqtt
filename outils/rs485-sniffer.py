#!/usr/bin/env python3
# =============================================================================
# Écoute passive d'un bus RS485 (aucune trame émise) : reconstitue et décode les trames Modbus RTU vues sur le fil,
# sans jouer le rôle de maître. C'est l'outil qui a permis de découvrir le protocole du DDZY422-D2 : le module Wi-Fi du
# compteur dialogue avec lui sur les mêmes bornes A/B ; un second adaptateur RS485 branché EN PARALLÈLE (A-A, B-B, rien
# à débrancher) montre les vraies requêtes et réponses (adresse, registres, valeurs).
#
# ⚠️ Ce script n'écrit JAMAIS sur le port série (aucun appel à ser.write) : sur un bus RS485 multi-maîtres, émettre en
# même temps qu'un maître légitime provoque une collision.
#
# Découpage en trames par la STRUCTURE Modbus, pas par le silence : un adaptateur USB-série livre les octets par paquets
# de 32 (à 9600 bauds, un paquet met déjà 37 ms à arriver), et le délai entre deux morceaux d'une même trame est du même
# ordre que le silence entre une requête et sa réponse : aucun seuil de temps ne les sépare. Le script accumule les
# octets et cherche une trame dont la longueur (requête de lecture = 8 octets, réponse = 5 + nombre d'octets annoncé,
# écriture multiple = 9 + nombre d'octets annoncé, exception = 5) ET le CRC16 sont corrects ; les octets qui ne forment
# aucune trame sont signalés à part. `flush_ms` (défaut 500) : silence après lequel des octets inutilisables sont
# abandonnés. Chaque réponse est associée à sa requête (adresse de départ des registres), les textes sont affichés.
#
# Prérequis : python3-serial (sudo apt install python3-serial)
#
# Usage :
#   python3 rs485-sniffer.py [device] [baudrate] [bytesize] [parity] [stopbits] [flush_ms]
#   python3 rs485-sniffer.py /dev/ttyUSB0 9600 8 E 1
# Valeurs par défaut : /dev/ttyUSB0, 9600, 8, E, 1 (réglage du DDZY422-D2 : adresse 1, 9600 bauds, parité paire).
# =============================================================================

import sys
import time
import datetime

try:
    import serial
except ImportError:
    print("Le paquet pyserial n'est pas installé. Sur Raspberry Pi OS :")
    print("  sudo apt-get install -y python3-serial")
    sys.exit(1)

device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
baudrate = int(sys.argv[2]) if len(sys.argv) > 2 else 9600
bytesize = int(sys.argv[3]) if len(sys.argv) > 3 else 8
parity_arg = (sys.argv[4] if len(sys.argv) > 4 else "E").upper()
stopbits = int(sys.argv[5]) if len(sys.argv) > 5 else 1
flush_ms = float(sys.argv[6]) if len(sys.argv) > 6 else 500.0

PARITY_MAP = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
STOPBITS_MAP = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}

# Temps-caractère approximatif (start + data + parité éventuelle + stop),
# suffisant pour calculer le seuil de silence — pas besoin d'exactitude au bit
# près pour détecter une coupure de trame.
bits_per_char = 1 + bytesize + (0 if parity_arg == "N" else 1) + stopbits
char_time_ms = (bits_per_char / baudrate) * 1000
silence_threshold_s = max(0.004, (char_time_ms * 3.5) / 1000)  # sert de délai de lecture (pas au découpage)
flush_s = flush_ms / 1000


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


FUNCTION_NAMES = {
    1: "Read Coils", 2: "Read Discrete Inputs",
    3: "Read Holding Registers", 4: "Read Input Registers",
    5: "Write Single Coil", 6: "Write Single Register",
    15: "Write Multiple Coils", 16: "Write Multiple Registers",
}


def describe_function(fc: int) -> str:
    if fc & 0x80:
        base = FUNCTION_NAMES.get(fc & 0x7F, f"fonction {fc & 0x7F}")
        return f"EXCEPTION de {base}"
    return FUNCTION_NAMES.get(fc, f"fonction {fc}")


# Dernière requête de lecture vue (adresse de départ, nombre de registres) : sert à situer la réponse suivante.
pending_read = None


def classify(buf: bytes):
    """('requête' | 'réponse' | None, détail) d'une trame de lecture (3/4) ou d'écriture multiple (16)."""
    fc = buf[1]
    if fc in (3, 4):
        if len(buf) == 8:
            return "requête", f"lecture de {int.from_bytes(buf[4:6], 'big')} registre(s) à partir de 0x{int.from_bytes(buf[2:4], 'big'):04x}"
        if len(buf) == 5 + buf[2]:
            return "réponse", f"{buf[2] // 2} registre(s)"
    if fc == 16:
        if len(buf) == 8:
            return "réponse", f"écriture acquittée à partir de 0x{int.from_bytes(buf[2:4], 'big'):04x}"
        if len(buf) >= 9 and len(buf) == 9 + buf[6]:
            return "requête", f"écriture de {int.from_bytes(buf[4:6], 'big')} registre(s) à partir de 0x{int.from_bytes(buf[2:4], 'big'):04x} = {buf[7:-2].hex()}"
    return None, ""


def readable_text(data: bytes):
    """Chaînes ASCII lisibles (au moins 4 caractères) contenues dans les données, ou None."""
    parts = [seg.decode("ascii") for seg in data.split(b"\x00") if len(seg) >= 4 and all(32 <= c < 127 for c in seg)]
    return " | ".join(parts) if parts else None


def flush_frame(buf: bytes, frame_count: int) -> int:
    global pending_read
    if not buf:
        return frame_count
    frame_count += 1
    now = datetime.datetime.now().isoformat()

    if len(buf) < 4:
        print(f"[{now}] #{frame_count} trame trop courte ({len(buf)} octets) : {buf.hex()}")
        return frame_count

    payload, received_crc_bytes = buf[:-2], buf[-2:]
    received_crc = received_crc_bytes[0] | (received_crc_bytes[1] << 8)
    computed_crc = crc16_modbus(payload)
    crc_ok = received_crc == computed_crc

    addr = buf[0]
    fc = buf[1]
    data = buf[2:-2]

    print(
        f"[{now}] #{frame_count} addr={addr} (0x{addr:02x}) fc={fc} ({describe_function(fc)}) "
        f"data={data.hex()} crc={'OK' if crc_ok else 'INVALIDE'} raw={buf.hex()}"
    )
    if crc_ok:
        role, detail = classify(buf)
        if role == "requête" and fc in (3, 4):
            pending_read = (int.from_bytes(buf[2:4], "big"), int.from_bytes(buf[4:6], "big"))
        if role:
            print(f"        ↳ {role} : {detail}")
        if role == "réponse" and fc in (3, 4) and pending_read and pending_read[1] * 2 == buf[2]:
            start = pending_read[0]
            words = [int.from_bytes(buf[3 + i:5 + i], "big") for i in range(0, buf[2], 2)]
            print("        ↳ registres : " + " ".join(f"{start + i:04x}={w:04x}" for i, w in enumerate(words)))
            text = readable_text(buf[3:-2])
            if text:
                print(f"        ↳ texte : {text}")
            pending_read = None
    return frame_count


def find_frame(buf: bytes):
    """Longueur de la trame Modbus RTU complète (CRC correct) qui commence au début de `buf`, sinon None."""
    if len(buf) < 5:
        return None
    fc = buf[1]
    lengths = []
    if fc & 0x80:
        lengths.append(5)                      # exception : adresse, code|0x80, code d'erreur, CRC
    if fc in (3, 4):
        lengths += [8, 5 + buf[2]]             # requête de lecture ; réponse (octets annoncés)
    if fc in (5, 6):
        lengths.append(8)                      # écriture simple : requête et écho identiques
    if fc == 16:
        lengths.append(8)                      # acquittement d'écriture multiple
        if len(buf) >= 7:
            lengths.append(9 + buf[6])         # requête d'écriture multiple
    for n in lengths:
        if len(buf) >= n:
            crc = crc16_modbus(buf[:n - 2])
            if crc == (buf[n - 2] | (buf[n - 1] << 8)):
                return n
    return None


def main():
    print(
        f"Écoute passive sur {device} @ {baudrate} {bytesize}{parity_arg}{stopbits} — "
        f"seuil de silence {silence_threshold_s * 1000:.1f}ms"
    )
    print("Ctrl+C pour arrêter. Aucune trame n'est émise sur le bus.\n")

    ser = serial.Serial(
        port=device,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=PARITY_MAP.get(parity_arg, serial.PARITY_EVEN),
        stopbits=STOPBITS_MAP.get(stopbits, serial.STOPBITS_ONE),
        timeout=silence_threshold_s,
    )

    buf = bytearray()
    frame_count = 0
    last_byte_time = time.monotonic()

    def drain(final: bool) -> None:
        """Extrait de `buf` toutes les trames complètes ; écarte les octets qui n'en forment aucune."""
        nonlocal frame_count
        while len(buf) >= 5:
            n = find_frame(bytes(buf))
            if n:
                frame_count = flush_frame(bytes(buf[:n]), frame_count)
                del buf[:n]
                continue
            if final or len(buf) > 300:
                # Aucune trame à cet endroit : on écarte UN octet et on recommence (resynchronisation).
                junk = buf[0]
                del buf[0]
                ignored.append(junk)
                continue
            break
        if final and buf:
            ignored.extend(buf)
            buf.clear()
        if ignored and (final or len(ignored) >= 1):
            now_s = datetime.datetime.now().isoformat()
            print(f"[{now_s}] {len(ignored)} octet(s) ne formant aucune trame valide (démarrage au milieu d'une trame, "
                  f"parasite ou mauvais réglage) : {bytes(ignored).hex()}")
            ignored.clear()

    ignored = bytearray()

    try:
        while True:
            chunk = ser.read(256)  # bloque au plus `timeout` secondes
            now = time.monotonic()
            if chunk:
                buf.extend(chunk)
                last_byte_time = now
                drain(False)
            elif buf and (now - last_byte_time) > flush_s:
                # Longue accalmie : ce qui reste n'est pas une trame complète.
                drain(True)
    except KeyboardInterrupt:
        print(f"\nArrêt — {frame_count} trame(s) capturée(s).")
    finally:
        ser.close()


if __name__ == "__main__":
    main()

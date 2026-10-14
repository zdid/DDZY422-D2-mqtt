#!/usr/bin/env bash
# =============================================================================
# Remplace un compteur DDSU666-H par un DDZY422-D2 sur la machine où le script est lancé (root), avec les MÊMES
# entités Home Assistant : même nœud, même thème MQTT, même nom d'appareil, énergies reprises du DDSU (décalages).
#
# À lancer SUR PLACE, dans une session SSH, depuis le dossier de l'agent (dépôt cloné, ou scripts/agents/ddzy422d2-mqtt/
# de l'image dimotic-ha) :
#     sudo ./outils/migrer-depuis-ddsu.sh --simulation     # ne change rien : montre ce qui serait fait
#     sudo ./outils/migrer-depuis-ddsu.sh                  # migration (une pause pour changer le câble RS485)
#     sudo ./outils/migrer-depuis-ddsu.sh --annuler        # retour au DDSU (remettre l'ancien câble)
#
# Disposition installée — la même que celle d'un agent déployé par dimotic-ha (guide §6-7) :
#     /dimotic-ha-addons/ddzy422d2-mqtt/{ddzy422d2-mqtt.py, ddzy422d2-mqtt.conf, ddzy422d2-mqtt.service, ancien-ddsu/}
#     /etc/systemd/system/ddzy422d2-mqtt.service  ->  lien vers le .service du dossier
#
# Le DDSU est arrêté, désactivé et MASQUÉ (impossible à relancer, même à la main) ; son unité et son script sont gardés
# dans ancien-ddsu/. Le DDZY est activé au démarrage et relancé en cas d'arrêt (Restart=always).
# =============================================================================
set -euo pipefail

NOM="ddzy422d2-mqtt"
DEST="/dimotic-ha-addons/${NOM}"
ANCIEN_SERVICE="ddsu666h-mqtt"
UNITE_ANCIEN="/etc/systemd/system/${ANCIEN_SERVICE}.service"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIMULATION=false; ANNULER=false
MQTT_HOTE=""; ADRESSE="1"; PARITE="E"; PERIPHERIQUE=""; NOEUD=""; THEME=""; NOM_APPAREIL=""; PREFIXE="homeassistant"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --simulation) SIMULATION=true ;;
    --annuler) ANNULER=true ;;
    --mqtt-host) MQTT_HOTE="$2"; shift ;;
    --adresse) ADRESSE="$2"; shift ;;
    --parite) PARITE="$2"; shift ;;
    --peripherique) PERIPHERIQUE="$2"; shift ;;
    --noeud) NOEUD="$2"; shift ;;
    --theme) THEME="$2"; shift ;;
    --nom) NOM_APPAREIL="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Option inconnue : $1" >&2; usage 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "ERREUR : à lancer en root (sudo)." >&2; exit 1; }
for c in python3 mosquitto_sub systemctl; do command -v "$c" >/dev/null || { echo "ERREUR : $c introuvable." >&2; exit 1; }; done

# Exécute (ou, en simulation, annonce) une commande qui change quelque chose.
agir() { if $SIMULATION; then echo "  [simulation] $*"; else "$@"; fi; }
etape() { echo; echo "=== $* ==="; }
unite_arg() {  # valeur d'une option « --xxx valeur » dans l'ExecStart d'une unité ; vide si absente
  local f="$1" opt="$2"
  [ -f "$f" ] || return 0
  grep -E '^ExecStart=' "$f" | python3 -c "import re,shlex,sys
t=shlex.split(sys.stdin.read().split('=',1)[1])
o=sys.argv[1]
print(t[t.index(o)+1] if o in t and t.index(o)+1<len(t) else '')" "$opt"
}

# --------------------------------------------------------------------------------------------------------------------
if $ANNULER; then
  etape "Retour au DDSU666-H"
  [ -f "${DEST}/ancien-ddsu/${ANCIEN_SERVICE}.service" ] || { echo "ERREUR : ${DEST}/ancien-ddsu/${ANCIEN_SERVICE}.service absent — rien à restaurer." >&2; exit 1; }
  agir systemctl disable --now "${NOM}" || true
  agir rm -f "/etc/systemd/system/${NOM}.service"
  agir systemctl unmask "${ANCIEN_SERVICE}"
  agir install -m 644 "${DEST}/ancien-ddsu/${ANCIEN_SERVICE}.service" "${UNITE_ANCIEN}"
  agir systemctl daemon-reload
  agir systemctl enable --now "${ANCIEN_SERVICE}"
  echo; echo "Fait. REMETTEZ l'ancien câble RS485 du DDSU666-H. Les énergies reviennent aux valeurs du DDSU."
  exit 0
fi

# --------------------------------------------------------------------------------------------------------------------
etape "1/7 Contrôles"
[ -f "${SOURCE}/ddzy422d2-mqtt.py" ] || { echo "ERREUR : ${SOURCE}/ddzy422d2-mqtt.py introuvable (lancer depuis le dossier de l'agent)." >&2; exit 1; }
[ -f "${UNITE_ANCIEN}" ] || [ "$(systemctl is-enabled "${ANCIEN_SERVICE}" 2>/dev/null)" = "masked" ] || { echo "ERREUR : service ${ANCIEN_SERVICE} introuvable : ce n'est pas la machine du DDSU ?" >&2; exit 1; }
[ "$(systemctl is-enabled "${ANCIEN_SERVICE}" 2>/dev/null)" = "masked" ] && { echo "Le DDSU est déjà masqué : migration déjà faite ? (voir --annuler)" >&2; exit 1; }
echo "Dossier de l'agent : ${SOURCE}"; echo "Service actuel : ${ANCIEN_SERVICE} ($(systemctl is-active "${ANCIEN_SERVICE}" || true))"

# Réglages du DDSU repris : options de son unité, sinon valeurs par défaut de son script, sinon réponse à la découverte MQTT.
[ -n "${MQTT_HOTE}" ] || MQTT_HOTE="$(unite_arg "${UNITE_ANCIEN}" --mqtt-host)"; MQTT_HOTE="${MQTT_HOTE:-127.0.0.1}"
[ -n "${PERIPHERIQUE}" ] || PERIPHERIQUE="$(unite_arg "${UNITE_ANCIEN}" --device)"; PERIPHERIQUE="${PERIPHERIQUE:-/dev/ttyUSB0}"
[ -n "${PREFIXE}" ] || PREFIXE=homeassistant
DECOUVERTE="$(mosquitto_sub -h "${MQTT_HOTE}" -t "${PREFIXE}/sensor/+/power/config" -v -W 4 2>/dev/null | grep -i ddsu || true)"
if [ -n "${DECOUVERTE}" ]; then
  eval "$(printf '%s\n' "${DECOUVERTE}" | head -1 | python3 -c "import sys,json,shlex
l=sys.stdin.read().split(' ',1)[1]; d=json.loads(l)
print('D_NOEUD=%s D_THEME=%s D_NOM=%s'%(shlex.quote(d['unique_id'].rsplit('_power',1)[0]),shlex.quote(d['state_topic'].rsplit('/state',1)[0]),shlex.quote(d['device']['name'])))")"
fi
NOEUD="${NOEUD:-${D_NOEUD:-}}"; THEME="${THEME:-${D_THEME:-}}"; NOM_APPAREIL="${NOM_APPAREIL:-${D_NOM:-}}"
[ -n "${NOEUD}" ] && [ -n "${THEME}" ] && [ -n "${NOM_APPAREIL}" ] || { echo "ERREUR : nœud, thème ou nom du DDSU introuvables (donnez --noeud, --theme, --nom)." >&2; exit 1; }
echo "Repris du DDSU : broker ${MQTT_HOTE}, port série ${PERIPHERIQUE}"
echo "                 nœud « ${NOEUD} », thème « ${THEME} », appareil « ${NOM_APPAREIL} »"

etape "2/7 Relevé des énergies du DDSU"
ETAT_DDSU="$(mosquitto_sub -h "${MQTT_HOTE}" -t "${THEME}/state" -C 1 -W 30 2>/dev/null || true)"
[ -n "${ETAT_DDSU}" ] || { echo "ERREUR : aucun état reçu sur ${THEME}/state en 30 s — le DDSU ne publie pas ?" >&2; exit 1; }
DDSU_IMPORT="$(printf '%s' "${ETAT_DDSU}" | python3 -c "import sys,json;print(json.load(sys.stdin)['energy_import'])")"
DDSU_EXPORT="$(printf '%s' "${ETAT_DDSU}" | python3 -c "import sys,json;print(json.load(sys.stdin)['energy_export'])")"
echo "DDSU : importé ${DDSU_IMPORT} kWh, exporté ${DDSU_EXPORT} kWh"

etape "3/7 Arrêt définitif du DDSU"
agir mkdir -p "${DEST}/ancien-ddsu"
agir install -m 644 "${UNITE_ANCIEN}" "${DEST}/ancien-ddsu/${ANCIEN_SERVICE}.service"
agir systemctl disable --now "${ANCIEN_SERVICE}"
agir rm -f "${UNITE_ANCIEN}"           # un fichier réel empêcherait « mask »
agir systemctl daemon-reload
agir systemctl mask "${ANCIEN_SERVICE}"
echo "DDSU arrêté, désactivé et masqué (unité gardée dans ${DEST}/ancien-ddsu/)."

etape "4/7 Câble RS485"
echo "Branchez maintenant le câble RS485 du DDZY422-D2 à la place de celui du DDSU666-H."
if ! $SIMULATION; then read -rp "Câble changé ? Appuyez sur Entrée pour continuer (Ctrl+C pour abandonner) " _; fi

etape "5/7 Installation de l'agent dans ${DEST}"
agir mkdir -p "${DEST}"
agir install -m 755 "${SOURCE}/ddzy422d2-mqtt.py" "${DEST}/ddzy422d2-mqtt.py"
[ -f "${SOURCE}/README.md" ] && agir install -m 644 "${SOURCE}/README.md" "${DEST}/README.md"
ecrire_conf() {  # $1 = décalage import, $2 = décalage export
  cat > "${DEST}/${NOM}.conf" <<EOF
# Généré par migrer-depuis-ddsu.sh le $(date +%F) — remplace un DDSU666-H (mêmes entités Home Assistant).
[ddzy422d2]
device = ${PERIPHERIQUE}
address = ${ADRESSE}
parity = ${PARITE}
interval = 5
module-wifi = auto
mqtt-host = ${MQTT_HOTE}
prefix = ${PREFIXE}
topic = ${THEME}
node = ${NOEUD}
name = ${NOM_APPAREIL}
convention-ddsu = oui
decalage-import = $1
decalage-export = $2
EOF
  chmod 600 "${DEST}/${NOM}.conf"
}
cat_unite() {
  cat > "${DEST}/${NOM}.service" <<EOF
[Unit]
Description=Compteur DDZY422-D2 (RS485) vers MQTT / Home Assistant — remplace le DDSU666-H (dimotic-ha)
After=network-online.target
Wants=network-online.target
Conflicts=${ANCIEN_SERVICE}.service

[Service]
ExecStart=/usr/bin/python3 ${DEST}/ddzy422d2-mqtt.py --config ${DEST}/${NOM}.conf
Environment=HOME=/root
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}

etape "6/7 Lecture à vide du DDZY : calcul des décalages d'énergie"
if $SIMULATION; then
  echo "  [simulation] lecture du DDZY (python3 ${SOURCE}/ddzy422d2-mqtt.py --once --dry-run …) puis décalage = total DDSU − total DDZY"
  DEC_IMPORT="0"; DEC_EXPORT="0"
else
  ECHEC=0
  SORTIE="$(python3 "${SOURCE}/ddzy422d2-mqtt.py" --device "${PERIPHERIQUE}" --address "${ADRESSE}" --parity "${PARITE}" --module-wifi auto --once --dry-run --topic "${THEME}" 2>&1)" || ECHEC=1
  ETAT_DDZY="$(printf '%s\n' "${SORTIE}" | grep -F "MQTT ${THEME}/state " | head -1 | sed "s#^MQTT ${THEME}/state ##" || true)"
  if [ "${ECHEC}" -ne 0 ] || [ -z "${ETAT_DDZY}" ]; then
    echo "ERREUR : le DDZY ne répond pas (câble, adresse ${ADRESSE}, parité ${PARITE}) :" >&2; printf '%s\n' "${SORTIE}" | tail -5 >&2
    echo "Pour revenir au DDSU : $0 --annuler" >&2; exit 1
  fi
  read -r DEC_IMPORT DEC_EXPORT < <(printf '%s' "${ETAT_DDZY}" | python3 -c "import sys,json
z=json.load(sys.stdin); print(round(${DDSU_IMPORT}-z['energy_import'],2), round(${DDSU_EXPORT}-z['energy_export'],2))")
  echo "DDZY brut : importé $(printf '%s' "${ETAT_DDZY}" | python3 -c "import sys,json;print(json.load(sys.stdin)['energy_import'])") kWh → décalages : import ${DEC_IMPORT} kWh, export ${DEC_EXPORT} kWh"
fi
if ! $SIMULATION; then ecrire_conf "${DEC_IMPORT}" "${DEC_EXPORT}"; cat_unite; else echo "  [simulation] écriture de ${DEST}/${NOM}.conf et ${DEST}/${NOM}.service"; fi

etape "7/7 Service DDZY (démarrage automatique, relance en cas d'arrêt)"
[ -f "/etc/systemd/system/${NOM}.service" ] && [ ! -L "/etc/systemd/system/${NOM}.service" ] && agir mv "/etc/systemd/system/${NOM}.service" "${DEST}/ancien-ddsu/${NOM}.service.preparee-$(date +%F)"
agir ln -sf "${DEST}/${NOM}.service" "/etc/systemd/system/${NOM}.service"
agir systemctl daemon-reload
agir systemctl enable --now "${NOM}"

if ! $SIMULATION; then
  echo "Vérification (jusqu'à 60 s)…"
  NOUVEL="$(mosquitto_sub -h "${MQTT_HOTE}" -t "${THEME}/state" -C 1 -W 60 2>/dev/null || true)"
  if [ -z "${NOUVEL}" ]; then echo "ATTENTION : rien reçu sur ${THEME}/state — voir : journalctl -u ${NOM} -n 30   (retour : $0 --annuler)" >&2; exit 1; fi
  printf '%s' "${NOUVEL}" | python3 -c "import sys,json
d=json.load(sys.stdin); print('État reçu : %s W, %s var, importé %s kWh, exporté %s kWh'%(d['power'],d['reactive_power'],d['energy_import'],d['energy_export']))"
  echo "DDSU avant : importé ${DDSU_IMPORT} kWh, exporté ${DDSU_EXPORT} kWh (les totaux doivent se suivre, à quelques dixièmes près)."
fi
echo; echo "Terminé. Le service ${NOM} est actif et redémarre seul. Retour au DDSU : $0 --annuler (+ ancien câble)."

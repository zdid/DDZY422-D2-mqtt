# DDZY422-D2-mqtt

Lit un compteur d'énergie monophasé **DDZY422-D2** (module Wi-Fi Solarman intégré) sur son bus **RS485 (Modbus RTU)** et
publie les mesures sur **MQTT**, avec **découverte automatique Home Assistant** : un appareil et 10 capteurs apparaissent
seuls. Les mesures publiées sont les mêmes que celles de [DDSU666-h-mqtt](https://github.com/zdid/DDSU666-h-mqtt), et les
options aussi : on passe d'un compteur à l'autre sans rien changer côté Home Assistant.

- Un seul fichier Python, **sans dépendance** : `python3` et `mosquitto_pub` (paquet `mosquitto-clients`).
- **Cohabite avec le module Wi-Fi du compteur** : le module interroge lui-même le compteur sur le même bus. Le script
  repère ses échanges et s'intercale entre eux, sans collision (voir plus bas).
- **Lecture seule** : le script n'envoie que des requêtes de lecture, il n'écrit jamais dans le compteur.

## Matériel

- Un compteur DDZY422-D2 (réglage observé : **adresse 1, 9600 bauds, 8E1**, parité paire).
- Un adaptateur **USB ↔ RS485** (puce CH340, CP2102…), relié **en parallèle** aux bornes **A** et **B** du compteur (les
  mêmes que celles du module Wi-Fi : rien à débrancher).

## Installation

```bash
sudo apt install python3 mosquitto-clients
sudo mkdir -p /opt/ddzy422d2-mqtt
sudo cp ddzy422d2-mqtt.py /opt/ddzy422d2-mqtt/
```

Essai (une lecture, sans rien envoyer à MQTT, avec l'affichage des messages) :

```bash
python3 ddzy422d2-mqtt.py --device /dev/ttyUSB0 --once --dry-run
```

Service systemd (adaptez les options dans `ddzy422d2-mqtt.service`) :

```bash
sudo cp ddzy422d2-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ddzy422d2-mqtt
```

## Options

Les réglages se donnent par la **ligne de commande**, par un **fichier de configuration**, ou les deux (voir plus bas).

| Option | Défaut | Rôle |
|---|---|---|
| `--config` | | fichier de configuration INI (voir plus bas) |
| `--device` | `/dev/ttyUSB0` | port série de l'adaptateur RS485 |
| `--baud` | `9600` | vitesse (2400, 4800, 9600, 19200, 38400) |
| `--parity` | `E` | parité : `E` (8E1) ou `N` (8N1) |
| `--address` | `1` | adresse Modbus du compteur |
| `--mqtt-host` / `--mqtt-port` | `127.0.0.1` / `1883` | broker MQTT |
| `--mqtt-user` | *(vide)* | nom d'utilisateur MQTT ; vide = connexion anonyme |
| `--mqtt-password` | *(vide)* | mot de passe MQTT (demande `--mqtt-user`) |
| `--prefix` | `homeassistant` | préfixe de découverte de Home Assistant |
| `--topic` | `ddzy422d2` | thème MQTT de l'état (`<thème>/state`) |
| `--node` | `ddzy422d2` | identifiant du nœud dans la découverte |
| `--name` | `Compteur DDZY422-D2` | nom de l'appareil dans Home Assistant |
| `--interval` | `5` | secondes entre deux lectures (1 à 60) |
| `--module-wifi` | `auto` | `auto` : s'intercale entre les échanges du module Wi-Fi s'il est présent ; `non` : pas de module, interrogation libre |
| `--topic`, `--node`, `--name` | `ddzy422d2`, `ddzy422d2`, `Compteur DDZY422-D2` | thème de l'état, identifiant du nœud (base des identifiants d'entités : `sensor.<nœud>_power`…) et nom de l'appareil dans Home Assistant ; un nom donné explicitement a priorité sur la valeur par défaut de `--convention-ddsu` |
| `--decalage-import`, `--decalage-export` | `0` | kWh ajoutés à l'énergie importée / exportée (l'énergie totale est recalculée) : pour **reprendre les totaux d'un compteur remplacé** et éviter un saut dans les statistiques |
| `--convention-ddsu` | | **remplacer un DDSU666-H sans rien changer dans Home Assistant** : puissance réactive = −√(S²−P²) (toujours négative) et facteur de puissance de signe opposé, comme le DDSU ; noms par défaut `ddsu666h` / `Compteur DDSU666-H` (mêmes entités, mêmes historiques). Mesuré sur 16 h en parallèle : la réactive brute du DDZY ne cadre pas avec sa propre puissance apparente et active (écart moyen 266 var), celle du DDSU est exactement −√(S²−P²) (0,1 var) |
| `--once` | | une seule lecture puis sortie |
| `--dry-run` | | affiche les messages au lieu de les envoyer |

Seule `--module-wifi` est propre à ce compteur ; les autres options sont celles de DDSU666-h-mqtt (les valeurs par défaut
de `--parity` et `--address` diffèrent, parce que le compteur est réglé autrement).

## Fichier de configuration

`--config fichier.conf` lit un fichier INI avec une section `[ddzy422d2]` (modèle : `ddzy422d2-mqtt.conf.example`). Les clés
sont les noms des options sans `--` (`mqtt-host` ou `mqtt_host`, indifféremment). **La ligne de commande a la priorité sur
le fichier, qui a la priorité sur les valeurs par défaut.**

```ini
[ddzy422d2]
address = 1
mqtt-host = 192.168.1.10
mqtt-user = compteur
mqtt-password = secret
interval = 5
```

```bash
sudo install -m 600 ddzy422d2-mqtt.conf.example /etc/ddzy422d2-mqtt.conf   # puis l'éditer
python3 ddzy422d2-mqtt.py --config /etc/ddzy422d2-mqtt.conf
```

**Mot de passe MQTT** : préférez le fichier (droits `600`, le script avertit s'il est lisible par d'autres). Sur la
ligne de commande, le mot de passe est visible dans la liste des processus de la machine (`ps`), y compris dans celle
de `mosquitto_pub` qui le reçoit en argument à chaque publication.

## Cohabitation avec le module Wi-Fi

Le module Solarman du compteur interroge celui-ci sur le bus RS485 :

- toutes les ~61 s, une rafale de lectures (34 registres, puis le modèle et la version) ;
- toutes les 30 s, un « battement » (lecture d'un registre et écriture d'un registre de service).

Deux maîtres qui parlent en même temps se brouillent. En mode `auto`, le script écoute le bus, repère la lecture
périodique du module, **se cale dessus** et saute les créneaux qui tomberaient pendant les échanges du module (environ
3 créneaux sur 12 à 5 s). Il n'émet que si le bus est silencieux depuis 150 ms. S'il ne voit aucun échange du module
pendant 90 s au démarrage, il interroge librement et prendra le module en compte s'il apparaît plus tard.

Avec un module Wi-Fi, ne faites **jamais** d'interrogation active qui ignore ces échanges : c'est ce qui rendait les
lectures impossibles avant d'avoir observé le bus.

## Ce qui est publié

L'état tient dans un seul message JSON sur `<thème>/state` :

```json
{"voltage": 238.8, "current": 2.737, "power": -14, "reactive_power": -639, "apparent_power": 653.0,
 "power_factor": -0.021, "frequency": 50.03, "energy_total": 4873.46, "energy_import": 6291.91, "energy_export": 1418.45}
```

| Clé | Mesure | Unité |
|---|---|---|
| `voltage` | tension | V |
| `current` | courant (valeur absolue) | A |
| `power` | puissance active (**positive = soutirage, négative = injection**) | W |
| `reactive_power` / `apparent_power` | puissance réactive / apparente | var / VA |
| `power_factor` | facteur de puissance | |
| `frequency` | fréquence | Hz |
| `energy_import` / `energy_export` | énergie importée / exportée | kWh |
| `energy_total` | importée − exportée | kWh |

Les capteurs de Home Assistant expirent (`expire_after`, six fois l'intervalle) : si le script s'arrête, ils passent en
« indisponible ». Les énergies s'utilisent directement dans le tableau Énergie.

## Registres lus

Le script lit les 34 registres 0x0000 à 0x0021 en une requête (fonction Modbus 3), comme le module Wi-Fi. Table déduite de
l'écoute passive du bus et comparée à un autre compteur (voir « Validation ») :

| Registre | Contenu | Échelle |
|---|---|---|
| `0x0001` | tension | ×0,1 V |
| `0x0002`–`0x0003` | courant | ×0,001 A |
| `0x0006`–`0x0007` | puissance active | W |
| `0x0008`–`0x0009` | puissance réactive | var |
| `0x000B` | puissance apparente | VA |
| `0x000C` | facteur de puissance (bit 15 = signe) | ×0,001 |
| `0x000D` | fréquence | ×0,01 Hz |
| `0x000E`–`0x000F` | énergie importée (somme des 4 tarifs `0x0010`–`0x0017`) | ×0,01 kWh |
| `0x0018`–`0x0019` | énergie exportée (somme des 4 tarifs `0x001A`–`0x0021`) | ×0,01 kWh |
| `0x0105`–`0x0107` | horloge du compteur | |
| `0x5000` | modèle (texte « DDZY422-D2 ») | |

**Signe** : pour les grandeurs signées, le registre qui précède la valeur vaut `0x8000` quand la valeur est négative
(0 sinon).

## Validation

Comparaison avec un DDSU666-H sur la **même ligne** pendant 17,3 h (10 111 paires de mesures à moins de 2,5 s) :

| Mesure | Écart DDZY − DDSU666-H |
|---|---|
| Tension | −0,02 V |
| Fréquence | −0,001 Hz |
| Puissance active | corrélation 0,9989 ; +7,4 W en moyenne ; +0,9 % au-dessus de 500 W |
| Courant / puissance apparente | +3,5 % (écart constant ; l'origine n'est pas déterminée) |
| Énergie importée | +9,95 kWh contre +9,87 kWh (0,8 %) |

Les grandes différences ponctuelles de puissance (43 mesures sur 10 111 au-delà de 150 W) viennent surtout d'instants de
lecture différents pendant une variation rapide. L'**énergie exportée** n'a été vérifiée que sur de très faibles valeurs
(+0,04 kWh contre +0,05 kWh) : l'attribution du compteur `0x0018` à l'export est probable mais **à confirmer** sur une
vraie période d'injection.

## Écoute passive (outil)

`outils/rs485-sniffer.py` écoute le bus sans jamais émettre et décode les trames Modbus : c'est ce qui a permis de
découvrir ce protocole. Prérequis : `python3-serial`.

```bash
python3 outils/rs485-sniffer.py /dev/ttyUSB0 9600 8 E 1
```

## Statut

Version **0.9.0**. Le script a été essayé sur le bus d'un compteur réel (écoute et interrogation à 5 s en
cohabitation avec le module Wi-Fi) ; la version publiée ici est une réécriture sans dépendance, validée contre un faux
compteur (trame réelle) mais pas encore exploitée en continu sur du matériel.

Ce dépôt est aussi le fournisseur de l'agent DDZY422-D2 du projet [dimotic-ha](https://github.com/zdid/dimotic-ha).

## Licence

MIT, voir [LICENSE](LICENSE).

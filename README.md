# MediaDeck

Télécommande web pour un PC multimédia Linux, pensée pour une tablette Android posée à côté.
Le PC exécute un petit pont Python ; la tablette n'a besoin que d'un navigateur.

*English version: [README-EN.md](README-EN.md)*

## Fonctions

| Onglet | Contenu | Côté PC |
|---|---|---|
| **Lecture** | Pochette, titre, progression, lecture/pause, suivant/précédent, aléatoire, répétition, volume du lecteur, choix du lecteur | MPRIS (D-Bus de session) : Elisa, VLC, mpv, Firefox, Chromium, Spotify, Kodi… |
| | **Barre de volume** sous le lecteur : volume principal, sourdine ; le bouton de la sortie active ouvre une fenêtre avec le choix de la sortie (HDMI, Bluetooth…) et le volume par sortie ; la barre reste toujours visible | `pactl` (PipeWire ou PulseAudio) |
| **Pavé** | Pavé tactile (clic, clic droit, défilement 2 doigts), croix directionnelle, Échap, Espace, F, F11…, saisie de texte avec le clavier de la tablette | `/dev/uinput` : fonctionne sous X11 **et** Wayland |
| **Lanceurs** | Boutons configurables : sites web et applications installées (page `/admin`) | navigateur, `gio launch` |
| **Système** | Verrouillage, veille, redémarrage, extinction (appui long 1 s) : opérations délicates, séparées des lanceurs | logind, `org.freedesktop.ScreenSaver` |

L'état est poussé en temps réel par WebSocket (signaux `PropertiesChanged`, `Seeked`, `pactl subscribe`) ; la position de lecture est interpolée côté tablette.

## Architecture

```
Tablette (navigateur plein écran)
        │  HTTP + WebSocket, jeton d'accès
        ▼
mediadeck (aiohttp, service systemd --user)
   ├─ mpris.py   → org.mpris.MediaPlayer2.*          (bus de session)
   ├─ audio.py   → pactl list/set-sink-*, subscribe
   ├─ input.py   → python-evdev / uinput             (clavier + pointeur virtuels)
   ├─ keymap.py / text.py → saisie de texte selon la disposition réelle du clavier (libxkbcommon)
   └─ system.py  → org.freedesktop.login1, ScreenSaver, lanceurs (base SQLite, page /admin)
```

KDE Connect n'est pas nécessaire : il peut rester installé pour ses autres fonctions.

## Installation (Debian / LMDE)

```bash
make deps             # paquets système (sudo) : venv, gcc, pactl, qrencode
make install          # .venv + dépendances Python
make config           # ~/.config/mediadeck/config.toml (mode 600) avec un jeton aléatoire
make uinput           # règle udev « uaccess » pour /dev/uinput (sudo)
make service-install  # service systemd utilisateur, démarré avec la session graphique
make url              # URL + QR code à scanner avec la tablette
```

Sur la tablette : scanner le QR code, puis menu du navigateur → « Ajouter à l'écran d'accueil ».
Le jeton est mémorisé puis retiré de la barre d'adresse.

Le service doit tourner **dans la session graphique** : il a besoin du bus D-Bus de session et du serveur audio de l'utilisateur.

## Configuration

`~/.config/mediadeck/config.toml` (voir [config.example.toml](config.example.toml)) :

```toml
[server]
host = "0.0.0.0"
port = 8765
token = "…"            # make token pour le régénérer
advertise_host = "192.168.1.104"   # optionnel : adresse du QR code (sinon détection automatique)
tls_cert = "~/.config/mediadeck/tls/server.pem"       # écrits par `make cert` (HTTPS)
tls_key = "~/.config/mediadeck/tls/server.key"
tls_ca = "~/.config/mediadeck/tls/mediadeck-ca.crt"   # servi sur /ca.crt pour la tablette
http_port = 8766       # optionnel : port HTTP en clair en plus du HTTPS (0 ou absent = désactivé)

[audio]
max_volume = 1.0       # plafond depuis la tablette

[features]
input = true
power = true
text_input = false       # clavier virtuel : voir « Saisie de texte » avant d'activer

[admin]
enabled = true                 # page /admin et API des lanceurs
allow_custom_command = false   # true : autoriser les commandes brutes depuis /admin

# Importés une seule fois dans la base au premier démarrage, puis gérés sur /admin
[[launchers]]
id = "youtube"
label = "YouTube TV"
icon = "tv"            # app, tv, film, globe, music, screen-off, speaker, lock, window
command = ["chromium", "--start-fullscreen", "https://www.youtube.com/tv"]
```

Les commandes des lanceurs sont des listes argv, jamais passées à un shell.

## Lanceurs

Les boutons de l'onglet **Lanceurs** sont stockés dans une base SQLite (`~/.local/share/mediadeck/launchers.db`,
mode 600). Le bouton **Gérer** de l'onglet ouvre la page `/admin` : ajouter, modifier, supprimer et réordonner
les lanceurs. Les changements apparaissent aussitôt sur la tablette.

| Type | Définition | Ce que le client peut écrire |
|---|---|---|
| **Site web** | URL http(s), navigateur (Chromium, Chrome, Brave, Firefox), plein écran | une URL |
| **Application installée** | choisie dans la liste des fichiers `.desktop`, lancée avec `gio launch` | rien (choix dans une liste) |
| **Commande** | liste d'arguments brute | tout : désactivé par défaut |

- **Sécurité** : l'API exige le jeton dans l'en-tête `Authorization: Bearer` (jamais dans l'URL). Une commande
  s'exécute avec vos droits : tant que `allow_custom_command = true` n'est pas dans la section `[admin]` de
  `config.toml`, créer ou modifier une commande est refusé (403). Une commande existante reste renommable.
- **Migration** : au premier démarrage, les `[[launchers]]` de `config.toml` sont importés une seule fois (type
  commande). Ils sont ensuite ignorés et peuvent être supprimés du fichier. Pour tout réimporter, supprimer la base.
- `enabled = false` désactive la page et l'API ; les lanceurs existants continuent de fonctionner.
- Le plein écran automatique n'existe pas avec Firefox, dont le mode kiosque ignore F11.
- `gio` (paquet `libglib2.0-bin`, installé par `make deps`) lance les applications installées.

## Saisie de texte

Le bouton **Clavier** de l'onglet Pavé ouvre une fenêtre de saisie. On tape avec le clavier de la tablette
(accents, correction, dictée vocale), puis **Envoyer** ou **Envoyer ↵** tape le texte dans la fenêtre active du PC.
Tab, Effacer et Entrée sont à côté du champ. La fenêtre n'est pas modale : le pavé tactile reste utilisable (cliquer
dans un champ du PC, puis taper). **Masquer le texte** affiche des points, pour un mot de passe.

Chaque caractère est traduit en touches selon la disposition **réelle** du clavier du PC (AZERTY, QWERTY…), lue dans
la configuration de KDE (`~/.config/kxkbrc`, sinon `setxkbmap -query`, la disposition active venant de D-Bus), avec
libxkbcommon. Maj, AltGr et touches mortes (ê, ñ…) sont gérés ; les touches partent par uinput comme celles d'un vrai
clavier. **Le presse-papiers n'est pas utilisé** : rien ne reste dans l'historique de Klipper, mots de passe compris.

- **Désactivée par défaut** : `[features] text_input = true` pour l'activer. C'est un clavier complet : avec le jeton,
  on peut ouvrir un terminal (touche Super) et y taper des commandes. Ne l'activez que si cela vous convient.
- 500 caractères par envoi (limite de taille d'un message WebSocket).
- Un caractère absent de la disposition (→, emoji…) est ignoré, le reste est tapé et une erreur le signale. Les
  apostrophes, guillemets, « … » et tirets typographiques sont remplacés par leur équivalent ASCII.
- L'état de Verr. Maj du PC n'est pas détecté : s'il est actif, les majuscules sont inversées.
- Les lettres accentuées par touche morte dépendent de la composition gérée par l'application (vérifié en simulation
  xkb, pas dans chaque application). Un moteur de saisie actif sur le PC (fcitx, ibus) peut modifier le résultat.
- Dépend de libxkbcommon et des données xkb (paquets `libxkbcommon0` et `xkb-data`, installés par `make deps`).

## Page des QR codes

`make qr` ouvre dans le navigateur du PC une page qui affiche, en grand, les codes à scanner avec la tablette :
accès **sécurisé** (HTTPS), accès **non sécurisé** (HTTP, si `http_port` est activé) et **certificat de la CA** à
installer. Seuls les codes utiles à la configuration en cours apparaissent. La langue suit `LANG`.

Les codes contiennent le jeton : la page n'est donc **jamais servie par le serveur** (un client du réseau pourrait la
lire). Elle est écrite en mode 600 dans `$XDG_RUNTIME_DIR/mediadeck/access.html` (vidé à la déconnexion) et ouverte en
`file://`. Elle s'appuie sur `qrencode` (installé par `make deps`) et ne dépend d'aucun script ni ressource externe.

## HTTPS local

Certains navigateurs mobiles exigent HTTPS, de même que l'API Wake Lock et l'installation propre en PWA.
MediaDeck crée sa propre autorité de certification (CA) locale avec `openssl` :

```bash
make cert                              # CA + certificat serveur, active HTTPS dans config.toml
systemctl --user restart mediadeck     # ou relancer `make dev`
```

Sur la tablette, **une seule fois** :

1. Ouvrir `https://<ip>:8765/ca.crt` (adresse et QR code affichés par `make cert`) et accepter l'avertissement
   de certificat : le fichier `mediadeck-ca.crt` est téléchargé.
2. Réglages → Sécurité → Chiffrement et identifiants → Installer un certificat → **Certificat d'autorité**,
   puis choisir ce fichier (le chemin exact varie selon la version d'Android).
3. Rescanner le QR code de `make url` : l'adresse est maintenant en `https://`, donc le jeton mémorisé pour
   l'ancienne adresse `http://` ne sert plus.

Notes :

- Le port ne parle plus que HTTPS : une requête `http://` sur ce port échoue. Pour garder aussi du HTTP en clair,
  voir « Choisir : sécurisé ou non » ci-dessous.
- La CA ne peut signer que des adresses privées (10/8, 172.16/12, 192.168/16, 127/8) et les noms `localhost`,
  `*.local`, `*.lan` et celui de la machine (`nameConstraints`). Sa clé `ca.key` reste sur le PC (mode 600) :
  ne pas la copier.
- Le certificat serveur est valable 825 jours. `make cert` le réémet sans changer la CA : rien à refaire sur
  la tablette. Si l'adresse du PC change, corriger `advertise_host` puis relancer `make cert`.
- Navigateur de la tablette : prendre un navigateur Chromium autonome (Chrome, Brave, Vivaldi, Vanadium…), qui fait
  confiance aux CA installées par l'utilisateur. Les navigateurs fondés sur le WebView système (DuckDuckGo…) peuvent
  les ignorer, et Firefox pour Android exige un réglage caché : non vérifié sur ce projet. Seule l'installation de
  la CA est globale ; le téléchargement de `/ca.crt` peut se faire depuis n'importe quel navigateur.
- Pour tout recommencer (nouvelle CA, à réinstaller sur la tablette) : supprimer `~/.config/mediadeck/tls/` et les
  trois lignes `tls_*` de `config.toml`, puis `make cert`.

### Choisir : sécurisé ou non

Deux modes d'accès, au choix de la tablette, et qui peuvent coexister :

| Mode | Adresse | Pour | Limites |
|---|---|---|---|
| **Non sécurisé** (HTTP) | `http://<ip>:<port>` | rien à installer ; marche avec tous les navigateurs | trafic et jeton en clair sur le LAN ; pas de Wake Lock ni de PWA |
| **Sécurisé** (HTTPS) | `https://<ip>:<port>` | trafic chiffré, Wake Lock, PWA | la CA doit être installée sur la tablette (voir plus haut) |

| Configuration (`[server]`) | Résultat |
|---|---|
| ni `tls_*` ni `http_port` | HTTP seul sur `port` (par défaut) |
| `tls_*` (`make cert`) | HTTPS seul sur `port` |
| `tls_*` + `http_port = 8766` | HTTPS sur `port` **et** HTTP en clair sur `http_port` |

Pour proposer les deux : `make cert` puis `make http-on` (écrit `http_port = 8766` ; `make http-on HTTP_PORT=9000`
pour un autre port), ouvrir ce port si besoin (`make firewall HTTP_PORT=8766`), puis redémarrer le service
(`systemctl --user restart mediadeck`, ou relancer `make run`). `make url` affiche alors **deux adresses et deux QR
codes**, l'un « Secure », l'autre « Unsecured ». Chaque adresse est une origine distincte : le jeton se mémorise
séparément pour chacune, et une PWA ne s'installe que depuis l'adresse HTTPS.

Pour retirer le HTTP en clair : `make http-off` (HTTPS seul). Pour revenir au HTTP seul : retirer aussi les trois
lignes `tls_*` de `config.toml` (les certificats restent dans `~/.config/mediadeck/tls/`). `/ca.crt` est servi sur les deux ports, ce qui permet de récupérer la CA sans
avertissement de certificat depuis le port HTTP.

## Commandes make

| Cible | Rôle |
|---|---|
| `make run` / `make dev` | lancer au premier plan (normal / verbeux) |
| `make check` | valider la configuration |
| `make token` | régénérer le jeton (les tablettes devront rescanner) |
| `make qr` | ouvrir dans le navigateur du PC une page avec les QR codes à scanner (HTTPS, HTTP, CA) |
| `make cert` | créer la CA locale et le certificat HTTPS, activer HTTPS |
| `make http-on` / `make http-off` | ajouter / retirer le port HTTP en clair (non sécurisé) à côté du HTTPS |
| `make status` / `make logs` | état et journal du service |
| `make firewall` | ouvrir le port aux réseaux privés avec ufw (`HTTP_PORT=8766` pour le port HTTP en clair) |
| `make lint` / `make test` | pyright / tests unitaires |
| `make service-uninstall`, `make uinput-remove`, `make distclean` | désinstallation |

## Sécurité

- `/ws` et `/art` exigent le jeton (comparaison à temps constant) ; la page HTML seule est publique.
- Le fichier de configuration est refusé s'il est lisible par le groupe ou les autres.
- `/art` ne sert que la pochette `file://` du lecteur demandé, et uniquement des images de moins de 10 Mo.
- L'API `/api/*` des lanceurs exige le jeton en en-tête `Authorization` (pas de CSRF possible depuis un autre site) ; les commandes brutes y sont interdites sauf `allow_custom_command = true` dans le fichier de configuration.
- Les messages WebSocket sont limités à 4 Ko ; les déplacements souris et défilements sont bornés.
- uinput est autorisé par ACL logind (`uaccess`) pour l'utilisateur de la session active, et non par le groupe `input`, qui donnerait accès en lecture à tous les claviers.
- Sans `make cert`, ou sur le port `http_port`, le trafic est en HTTP clair (jeton compris) : réservé au LAN de confiance. Avec HTTPS, il est chiffré, mais le port ne doit toujours pas être exposé hors du LAN : pour un accès distant, passer par WireGuard. `http_port` est désactivé par défaut.

## Limites connues

- **Écran toujours allumé** : l'API Wake Lock exige HTTPS (voir « HTTPS local »). En HTTP, régler la mise en veille de la tablette, ou utiliser un navigateur kiosque (Fully Kiosk Browser…).
- **VPN sur la tablette** : un VPN actif sur l'appareil client coupe l'accès au réseau local, et la page expire (« site inaccessible, trop de temps à répondre »). Autoriser les connexions LAN dans l'application VPN (Proton VPN : Paramètres → Connexion → Paramètres avancés → Connexions LAN), désactiver le « verrouillage » / kill switch d'Android, ou couper le VPN.
- **Saisie de texte** : voir la section dédiée ; désactivée par défaut, et limitée aux caractères que la disposition du clavier du PC permet de produire.
- **Verrouillage / extinction** : dépendent des règles polkit. Une session locale active est autorisée par défaut sur Debian.
- Certains lecteurs (navigateurs notamment) n'exposent ni volume, ni aléatoire, ni répétition : les contrôles correspondants sont alors masqués.

## Licence

MIT

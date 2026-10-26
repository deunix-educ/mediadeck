# CLAUDE.md — MediaDeck

Contexte pour reprendre le projet avec Claude Code (VSCodium). À lire avant toute modification.

## Objectif

Télécommande web pour un **PC multimédia Linux** (Debian / LMDE, KDE Plasma), utilisée depuis une
**tablette Android** posée à côté. Le PC exécute un pont Python ; la tablette n'a besoin que d'un
navigateur en plein écran. KDE Connect a été écarté : MPRIS, PipeWire, uinput et logind sont pilotés
directement, ce qui donne plus de contrôle et une interface libre.

## Conventions (obligatoires)

- Code, identifiants et messages de log/erreur en **anglais** ; commentaires et docstrings en **français**.
- Indentation **4 espaces** partout (Python, JS, CSS, HTML, JSON, TOML). Seule exception : les recettes du Makefile (tabulations).
- JavaScript : **classes** ES, modules natifs, aucune dépendance ni étape de build.
- Python ≥ 3.11, typé ; **pyright** doit rester à 0 erreur (`make lint`).
- Documentation double : `README.md` (fr) et `README-EN.md` (en), à garder synchronisés.
- Toutes les opérations système passent par le **Makefile**.
- `.gitignore` doit exclure tout secret ; `config.toml` contient le jeton et ne doit jamais être commité.
- Fins de ligne LF (`.gitattributes`, `.editorconfig`).

## Commandes

```bash
make install      # .venv + dépendances (+ pyright)
make config       # ~/.config/mediadeck/config.toml (mode 600) + jeton aléatoire
make dev          # serveur au premier plan, logs debug
make lint test    # pyright + unittest
make url          # URL tablette + QR code
make uinput       # règle udev uaccess (sudo)
make service-install / status / logs
```

## Architecture

```
mediadeck/
├── __main__.py   CLI : --config, --check, --url, -v ; lance web.run_app
├── config.py     TOML → dataclasses gelées ; validation stricte ; refuse un fichier lisible par group/other
├── server.py     aiohttp : /, /static/*, /ws (jeton), /art (jeton) ; diffusion d'état anti-rebond 100 ms + toutes les 10 s ;
│                 open_sites()/serve() : port principal (HTTPS si TLS) + port HTTP en clair facultatif (`http_port`)
├── mpris.py      MprisManager : découverte via NameOwnerChanged, PropertiesChanged + Seeked ; Player.execute()
├── audio.py      AudioManager : `pactl -f json list sinks`, `pactl subscribe` (relancé si mort), set-sink-*
├── input.py      VirtualInput : 2 périphériques uinput (clavier + pointeur), touches en liste blanche + TYPING_KEYS (saisie)
├── keymap.py     disposition réelle (kxkbrc / setxkbmap / D-Bus KDE) -> table keysym -> frappes (libxkbcommon via ctypes)
├── text.py       TextTyper : texte -> frappes uinput, verrou, relâchement garanti, 500 caractères max
├── system.py     SystemControl : logind (suspend/reboot/poweroff), ScreenSaver.Lock, lancement des lanceurs
├── launchers.py  LauncherStore (SQLite), validation, build_argv(), applications .desktop, navigateurs
├── admin.py      API REST /api/* des lanceurs (jeton en en-tête Authorization: Bearer)
├── qrpage.py     page HTML locale des QR codes (`make qr`) : qrencode -t SVG, images en data: URI, écrite en 600 dans $XDG_RUNTIME_DIR
├── tls.py        HTTPS local : CA privée + certificat serveur (CLI openssl), contexte SSL, enable_tls()
└── web/
    ├── index.html        sprite SVG + 4 panneaux (player + audio, remote, launchers, system)
    ├── admin.html        page /admin : liste + dialogue d'édition des lanceurs
    └── static/common.js  Storage, I18n(messages), $ : partagés par app.js et admin.js
        static/admin.js   Api, LauncherList, LauncherForm, AdminPage ; static/admin.css
        static/app.js     Throttle, Connection, PlayerPanel, AudioPanel,
                          Touchpad, RemotePanel, HoldButton, LauncherPanel, SystemPanel, Toast, Tabs, ScreenKeeper, App
```

Tout tourne dans **une seule boucle asyncio**. Le service doit s'exécuter **dans la session graphique**
(service `systemd --user`, `WantedBy=graphical-session.target`) pour accéder au bus D-Bus de session et à PipeWire.

## Protocole WebSocket

Connexion : `ws://host:port/ws?token=…` (HTTP 401 sinon). Messages JSON ≤ 4 Ko.

Client → serveur :

```json
{"domain": "player", "action": "play_pause|play|pause|stop|next|previous|seek|set_position|set_volume|shuffle|loop|raise", "player": "<bus name>", "value": …}
{"domain": "audio",  "action": "set_volume|toggle_mute|set_default", "sink": "<name|null=défaut>", "value": 0.0-max_volume}
{"domain": "input",  "action": "key", "key": "<nom de KEY_NAMES>"}
{"domain": "input",  "action": "move|scroll", "dx": int, "dy": int}
{"domain": "input",  "action": "click", "button": "left|right|middle"}
{"domain": "input",  "action": "text", "text": "<1 à 500 caractères>"}   // features.text_input ; erreur renvoyée pour les caractères impossibles
{"domain": "system", "action": "power", "kind": "suspend|reboot|poweroff"}
{"domain": "system", "action": "lock|launch", "id": "<launcher id>"}
```

- `seek` est relatif, en µs. Le client l'utilise de préférence à `set_position`, qui exige un trackid valide.
- Volumes en flottants 0..1. Positions et durées MPRIS en **microsecondes**.

Serveur → client :

- `{"type":"state", "mpris":{players, active, available}, "audio":{…}, "input":{available, reason, keys, text:{available, reason}}, "system":{…}}`
- `{"type":"error","message":"…"}`

### API REST des lanceurs (`admin.enabled`)

`Authorization: Bearer <jeton>` obligatoire (401 sinon) ; le jeton en paramètre d'URL est refusé. Corps JSON ≤ 16 Ko, erreurs `{"error": "…"}`.

```
GET    /api/launchers          {launchers:[{id,label,icon,kind,spec}], icons, kinds, browsers:[{id,name,installed,fullscreen}], allow_command}
POST   /api/launchers          {label, icon, kind, spec} → 201 (id généré à partir du nom)
PUT    /api/launchers/{id}     idem → 200 ; 404 si inconnu
DELETE /api/launchers/{id}     → 204
POST   /api/launchers/reorder  {ids:[…]} (tous les ids exactement une fois) → 204
GET    /api/apps               [{id: "x.desktop", name}]
GET    /admin                  page HTML (publique comme /, l'API exige le jeton)
```

`kind`/`spec` : `web` → `{url, browser, fullscreen}` ; `app` → `{desktop_id}` ; `command` → `{argv:[…]}`.
403 pour une commande brute si `admin.allow_custom_command` est faux (sauf commande existante inchangée : renommage OK).
Chaque modification déclenche `notify()` : les tablettes reçoivent la nouvelle liste (`system.launchers`, `system.admin`) sans recharger.

La pochette locale (`file://`) n'est jamais transmise : elle est remplacée par `/art?player=…&v=<hash>`, auquel le client ajoute le jeton.

## Décisions et raisons

- **aiohttp plutôt que Django** : un seul processus, pas de base de données ; Channels serait surdimensionné.
- **pactl plutôt que wpctl** : sortie JSON stable et `subscribe` pour les événements ; compatible pipewire-pulse.
- **uinput plutôt que xdotool/ydotool** : fonctionne sous Wayland. Deux périphériques distincts pour une bonne classification par libinput.
- **Règle udev `TAG+="uaccess"`** plutôt que le groupe `input`, qui permettrait de lire tous les claviers.
- **Saisie de texte par traduction de la disposition** (remplace l'ancien « pas de saisie de texte » : le piège était la disposition, pas la saisie). 
  Chaque caractère devient la/les touches que produirait la disposition réelle du PC : lecture de `kxkbrc` (sinon `setxkbmap -query`), index actif via `org.kde.keyboard`, 
  compilation par libxkbcommon (`ctypes`), table keysym -> (touche, Maj, AltGr), touches mortes par décomposition NFD (ê = `dead_circumflex` + e), accents isolés = touche morte + espace.
  **Pas de presse-papiers** (Ctrl+V) : Klipper garde un historique en clair, mots de passe compris, et seul le Ctrl+V fonctionne (pas dans un terminal). 
  Touches autorisées pour la saisie : `TYPING_KEYS` (bloc principal + `<>` + AltGr), contrôlées dans `VirtualInput.write_keys` ; le client ne choisit jamais un code, seulement du texte.
- **`features.text_input` désactivé par défaut** : un clavier complet donne le contrôle total du PC à qui a le jeton (Super + saisie = terminal), même logique que `allow_custom_command`. 
  Limite de 500 caractères : une trame WebSocket est limitée à 4 Ko et 500 × 4 octets (emoji) = 2 Ko ; au-delà aiohttp coupe la connexion.
- **Onglets Lecture et Son fusionnés** : le panneau `player` contient le lecteur MPRIS puis une barre de volume (sourdine, curseur du volume principal, bouton au nom de la sortie active qui ouvre la liste des sorties). Raisons : libérer un emplacement dans la barre d'onglets (4 au lieu de 5) et garder le volume à portée du lecteur. `PlayerPanel` et `AudioPanel` restent deux classes, protocole WebSocket inchangé (`domain: audio`). Un onglet `audio` mémorisé par un client retombe sur `player` (`Tabs.select`). Audio indisponible : la barre disparaît et une ligne `audio.none` la remplace.
  **Mise en page : la barre de volume doit rester visible quelle que soit la hauteur** (premier essai sous-optimal : panneau qui défile, barre sous la barre d'onglets dès ~640 px de haut, donc « masquée » sur la tablette). Donc `.panel-stack` (colonne flex, `overflow: hidden`) : le lecteur (`flex: 1 1 0; min-height: 0; overflow-y: auto`) prend la place restante et défile en interne si besoin, la barre (`z-index: 2`, la pochette est positionnée et peindrait par-dessus) est toujours en bas. Pochette carrée dimensionnée par la hauteur réellement disponible : `width: min(100%, 100cqh)` (`container-type: size` sur `.now-playing`, repli `100dvh - 330px`), désactivé en portrait où la pile est naturellement haute (`30dvh`). Liste des sorties = fenêtre flottante (`position: absolute; bottom: calc(100% + 8px)`, `max-height: 100dvh - 290px`, défilement interne) : l'ouvrir ne déplace ni ne comprime le lecteur ; fermée par le bouton, un clic extérieur ou Échap. Paliers de hauteur (`max-height: 700px` / `560px` en paysage) : bouton lecture et marges réduits, puis volume propre du lecteur et album masqués. Sur ≤ 520 px le nom de la sortie est masqué (libellé en `aria-label`). `align-items: safe center` sur la grille pour que le contenu qui dépasse reste atteignable par défilement.
- Fenêtre de saisie **non modale** (`dialog.show()`) : le pavé reste utilisable, et un `showModal()` assombrirait les toasts (hors couche supérieure).
- **Imports dbus-next par sous-modules** (`dbus_next.signature`, `dbus_next.aio.message_bus`, `dbus_next.constants`) : sinon pyright signale `reportPrivateImportUsage`.
- Les proxies dbus-next sont typés `Any`, car leurs méthodes `call_*` / `on_*` sont générées dynamiquement.
- **Page des QR codes = fichier local, jamais une route du serveur** (`make qr`, `--qr`, module `qrpage.py`) : les QR codes contiennent le jeton, une route `/qr` le donnerait à tout le LAN (même limitée à la boucle locale, elle serait fragile derrière un proxy). Page autonome écrite en 600 dans `$XDG_RUNTIME_DIR/mediadeck/access.html` (tmpfs) et ouverte en `file://` : pas de CSP à contourner (`<style>` inline permis hors serveur), pas d'avertissement de certificat, fonctionne serveur arrêté. `qrencode -t SVG` plutôt qu'une bibliothèque JS (aucune dépendance) ; le SVG reçoit `shape-rendering="crispEdges"`, sinon des lignes claires apparaissent entre les modules à l'échelle. Le jeton n'apparaît jamais en texte dans la page, seulement dans les QR codes. Le code du certificat de la CA passe par le port HTTP s'il existe (pas d'avertissement à accepter).
- **HTTPS par CA privée + `openssl`** plutôt que mkcert : aucune dépendance à installer, et la CA reçoit des `nameConstraints` (réseaux privés, `localhost`, `*.local`, `*.lan`, nom de la machine). La tablette fait confiance à cette CA : si sa clé fuitait, elle ne pourrait pas usurper un site public. Clés EC P-256, CA 10 ans, certificat serveur 825 jours (réémis sans toucher à la CA).
- **Choix sécurisé / non sécurisé = deux écoutes simultanées** (`server.http_port`, 0 par défaut) plutôt qu'un interrupteur : la tablette choisit son adresse sans redémarrer le service, et le HTTP sert de repli quand son navigateur ignore la CA (voir Pièges). `port` garde son rôle (HTTPS dès que `tls_cert` est défini, sinon HTTP) ; `http_port` exige TLS (sans lui, `port` est déjà en HTTP) et doit différer de `port`. Les deux sites partagent **un seul `AppRunner`** (`web.run_app` ne gère pas deux ports avec des réglages TLS différents), donc la même application, la même boucle et le même jeton. Désactivé par défaut car le jeton circule alors en clair sur le LAN ; `make cert` ne l'active pas (il suggère `make http-on`) ; `make http-on [HTTP_PORT=…]` / `make http-off` appellent `--http-port N` (`tls.set_http_port` : regex sur la ligne `port`, relecture par `load_config`, fichier restauré si le résultat est invalide). Activer ce port à la main dans le TOML était trop fragile : un premier essai de l'utilisateur est resté « non concluant » faute de la ligne `http_port`. `make url` affiche deux adresses et deux QR codes ; `make firewall HTTP_PORT=…` ouvre le second port.
- La CA publique est servie sur `/ca.crt` par le serveur lui-même (route publique, uniquement si `tls_ca` est défini) : la tablette l'installe après avoir accepté l'avertissement une fois, sans second port ni pare-feu à ouvrir (la route existe aussi sur `http_port` : récupération de la CA sans avertissement, avec la même absence d'authentification qu'un avertissement accepté).
- **CSP stricte** (`script-src 'self'`, `style-src 'self'`) : aucun attribut `style=""` ni script inline dans le HTML. En JS, utiliser `el.style.setProperty()`, qui est autorisé.
- Lanceurs : uniquement des listes argv, jamais de shell, avec `start_new_session=True`.
- **Lanceurs en base SQLite** (`~/.local/share/mediadeck/launchers.db`, mode 600, module standard `sqlite3`, aucune dépendance) plutôt qu'en TOML : modifiables à chaud depuis `/admin` sans redémarrer ni éditer un fichier. Les `[[launchers]]` de `config.toml` ne servent qu'à un **import unique** (drapeau `meta.seeded`), puis sont ignorés.
- **Sécurité de l'administration** : un lanceur « commande » = exécution de code avec les droits de l'utilisateur pour quiconque a le jeton. Donc trois types : `web` (URL http(s) + navigateur d'une liste fermée) et `app` (fichier `.desktop` choisi dans une liste, lancé par `gio launch <chemin résolu côté serveur>`) n'acceptent aucune commande du client ; `command` exige `[admin] allow_custom_command = true` dans `config.toml`, fichier que le client ne peut pas modifier. La commande est reconstruite au lancement depuis la base (`build_argv`), jamais fournie par le message WebSocket.
- API REST avec jeton en **en-tête** `Authorization` (pas dans l'URL : journaux, historique) ; un en-tête personnalisé n'est pas ajoutable par un site tiers sans CORS, donc pas de CSRF.
- **Onglet Lanceurs distinct de l'onglet Système** : les lanceurs (usage fréquent, sans risque) ne côtoient plus verrouillage/veille/redémarrage/extinction (rares, destructifs, appui long), pour éviter un mauvais appui. Pur changement d'UI : le protocole WebSocket est inchangé (`system.launchers` / `system.admin` dans l'état, action `system` + `launch`). `LauncherPanel` et `SystemPanel` sont deux classes ; `tabbar` en `grid-auto-flow: column` (nombre d'onglets libre).
- `common.js` : `Storage`/`I18n`/`$` extraits d'`app.js` pour être partagés avec `admin.js` (l'`I18n` prend les messages en paramètre ; la langue choisie est commune aux pages).
- Actions d'alimentation : appui long de 1 s côté UI (`HoldButton`).

## État (2026-10-23)

Vérifié :
- Saisie de texte : 87 tests (dont rejeu des frappes dans un état xkb avec composition : ASCII sur us/fr/de, texte français, accents par touches mortes) ; dans Chromium, 19 vérifications (fenêtre non modale, Envoyer / Envoyer ↵, touches rapides, masquage, caractère impossible, téléphone sans débordement) et rejeu des touches enregistrées : `Bonjour, ça va ? @ 12 € ñ` redonne exactement ce texte sur AZERTY latin9. Frappes réelles via uinput non essayées (elles iraient dans le vrai bureau).
- Choix sécurisé / non sécurisé (`http_port`, 2026-10-26) : pyright 0 erreur, 107 tests (dont `tests/test_serve.py` : HTTPS avec la CA locale et HTTP en clair côte à côte, chaque port gardant son protocole, libération du port principal si le second est occupé) ; vrai serveur en config isolée : `/` en 200 sur les deux ports, HTTPS refusé sans CA (curl 60), HTTP sur le port TLS refusé, `/ws` 401 sans jeton des deux côtés, WebSocket `wss://` et `ws://` avec jeton, arrêt SIGTERM propre (code 0, ports libérés). Le service systemd et la tablette réelle ne sont pas essayés.
- pyright : 0 erreur ; 59 tests unitaires OK (dont une vraie poignée de main TLS avec la CA locale, le stockage SQLite, la validation, l'API REST via `aiohttp.test_utils`).
- Fusion Lecture + Son (2026-10-26) : vérifiée dans Chromium piloté par DevTools avec un serveur factice servant la vraie interface (état de démonstration : lecteur + 3 sorties) : paysage 1280×800, 1280×640, 1024×600, 900×500, 800×412, portrait 800×1280, téléphone 412×915, avec et sans lecteur, avec et sans audio, une seule sortie ; barre de volume visible au-dessus de la barre d'onglets dans tous les cas ; ouverture/fermeture de la fenêtre des sorties (bouton, clic extérieur, Échap), mute, curseur, choix de sortie, lecture, bascule de langue, ancien onglet `audio` mémorisé, aucune erreur JavaScript. Correction au passage : l'ombre de la pochette grisait le titre en portrait (`.track { position: relative }`). Non essayé sur la vraie tablette.
- **UI dans un vrai Chromium** (headless, piloté par le protocole DevTools, serveur isolé) : télécommande + `/admin`, 28 vérifications : barre de 5 onglets (aussi à 360 px, sans débordement), onglet Système réduit aux opérations de session, création web/app, édition d'une commande importée (zone verrouillée), réordonnancement, suppression, bascule de langue, jeton absent/refusé, mise à jour en direct de la grille par WebSocket, et **lancements réels** des trois types (navigateur factice, `.desktop` via `gio`, commande). Aucune erreur JavaScript.
- HTTPS de bout en bout avec le vrai serveur (config isolée) : `/` avec la CA, rejet sans CA (curl 60), `/ca.crt`, `/ws` 401 sans jeton, WebSocket `wss://` avec jeton.
- Intégration avec `dbus-run-session` et un faux lecteur MPRIS (dbus-next ServiceInterface) : état, métadonnées, `/art` avec et sans jeton, play_pause, set_volume, seek, erreurs, refus 401, disparition du lecteur.

**Non vérifié sur une vraie machine :**
- rendu de l'UI dans un navigateur ;
- installation de la CA sur Android et chargement de la page en HTTPS sur la tablette ;
- pactl, uinput, logind et ScreenSaver.Lock ;
- service systemd et règle udev.

## Prochaines étapes envisagées

1. Test réel sur le PC multimédia et la tablette ; ajuster les gestes du pavé (gain, seuils dans `Touchpad`).
2. ~~HTTPS avec un certificat local~~ — fait (`make cert`, voir `tls.py`) ; reste à tester sur la tablette (installation de la CA, Wake Lock, PWA).
3. Essayer la saisie de texte sur la tablette réelle et dans les applications visées (champ de recherche du navigateur, connexion à un service), puis sous Wayland.
4. Test d'intégration versionné : déplacer le scénario `dbus-run-session` + faux lecteur dans `tests/` (cible `make test-integration`).
5. Versionner le test d'interface dans `tests/` (cible `make test-ui`) : pilote DevTools (Node, WebSocket natif) + Chromium headless + serveur isolé (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, faux `chromium` dans le `PATH`, faux `.desktop`), aujourd'hui seulement joué à la main.
6. Liste de lecture / file d'attente (`org.mpris.MediaPlayer2.TrackList`, si exposée par le lecteur).
7. Raccourcis KDE via D-Bus (`org.kde.kglobalaccel`) ou `kscreen-doctor` pour la sortie vidéo.
8. Éventuellement : documentation mkdocs fr/en si le projet grossit.

## Pièges

- Un VPN actif sur l'appareil client (Proton VPN sur le Pixel 8a de l'utilisateur) bloque le LAN : la page expire (ERR_CONNECTION_TIMED_OUT) alors que le serveur répond correctement depuis le PC. Réglage Proton : Paramètres → Connexion → Paramètres avancés → Connexions LAN ; vérifier aussi « Bloquer les connexions sans VPN » dans les réglages VPN d'Android.
- Android : les apps visant l'API 24+ (WebView système, donc probablement DuckDuckGo) ignorent les CA utilisateur sauf opt-in ; les navigateurs Chromium autonomes (Chrome, Brave, Vanadium) les respectent, Firefox demande un réglage caché. Non vérifié sur la tablette de l'utilisateur (DuckDuckGo a de plus traité `https://192.168.1.104:8765/ca.crt` saisi à la main comme une recherche).
- Passer en HTTPS change l'origine : le jeton mémorisé (localStorage) et une PWA installée en `http://` ne servent plus, la tablette doit rescanner le QR code. Le port principal ne répond plus qu'en TLS ; avec `http_port`, l'adresse HTTP en clair reste une origine distincte (jeton mémorisé à part, pas de Wake Lock ni de PWA).
- Si `tls_cert` pointe vers un fichier absent, `--check` et le démarrage échouent (`make cert` le recrée : il passe avant la validation TLS). Après un changement de nom de machine, le garde-fou `openssl verify` de `make cert` signale que la CA existante ne couvre plus ce nom : supprimer `~/.config/mediadeck/tls/`.
- Les lanceurs vivent dans la base : modifier `[[launchers]]` dans `config.toml` après le premier démarrage n'a aucun effet (le journal l'indique). Pour réimporter : supprimer `~/.local/share/mediadeck/launchers.db`. Avec `admin.enabled = false`, seule la base (`sqlite3`) permet encore de les modifier.
- Les lanceurs importés sont de type `command` : ils restent renommables sans `allow_custom_command`, mais leur commande n'est modifiable qu'avec lui. Pour s'en passer, les recréer en `web`/`app` depuis `/admin`.
- Les applications installées sont lancées par `gio launch` (paquet `libglib2.0-bin`, ajouté à `make deps`) ; la liste vient des `.desktop` de `XDG_DATA_DIRS` + exports Flatpak : un service systemd utilisateur sans `XDG_DATA_DIRS` retombe sur `/usr/local/share:/usr/share`.
- Saisie de texte : Verr. Maj du PC non détecté (majuscules inversées s'il est actif) ; un moteur de saisie (fcitx/ibus) peut altérer le résultat ; la composition des touches mortes dépend de l'application (vérifiée par simulation xkb avec la table de composition `en_US.UTF-8`, pas dans chaque application). Testé en session X11 ; Wayland utilise les mêmes sources (kxkbrc) mais n'a pas été essayé.
- Chromium déjà ouvert : un lanceur `web` peut s'ouvrir dans la fenêtre existante (le plein écran n'est alors pas garanti).
- `make url` détecte l'IP via la route par défaut : avec un VPN actif (WireGuard), il annonce l'IP du VPN. Renseigner `server.advertise_host` dans `config.toml` pour forcer l'adresse du QR code.
- `firefox --kiosk` (et `chromium --kiosk`) ignore F11 : impossible de quitter le plein écran depuis la télécommande. Le lanceur YouTube TV utilise `chromium --start-fullscreen`, que F11 permet de quitter.
- Lancé hors session graphique (ssh, cron), il n'y a pas de bus de session : MPRIS et système sont désactivés (log d'erreur) mais le serveur démarre quand même.
- `/dev/uinput` nécessite `modprobe uinput` et une reconnexion de session après `make uinput` ; vérifier avec `getfacl /dev/uinput`.
- Les navigateurs exposent souvent MPRIS sans `Volume` / `Shuffle` / `LoopStatus` : l'UI masque ces contrôles quand la valeur est `null`.
- La position MPRIS n'est pas signalée par `PropertiesChanged` : elle est relue explicitement (`refresh_position`).

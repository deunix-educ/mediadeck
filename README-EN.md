# MediaDeck

Web remote for a Linux media PC, designed for an Android tablet sitting next to it.
The PC runs a small Python bridge; the tablet only needs a browser.

*Version française : [README.md](README.md)*

## Features

| Tab | Content | PC side |
|---|---|---|
| **Playing** | Cover art, title, progress, play/pause, next/previous, shuffle, repeat, player volume, player selection | MPRIS (session D-Bus): Elisa, VLC, mpv, Firefox, Chromium, Spotify, Kodi… |
| **Sound** | Master volume, mute, output selection (HDMI, Bluetooth…) and per-output volume | `pactl` (PipeWire or PulseAudio) |
| **Touchpad** | Touchpad (click, right click, 2-finger scroll), D-pad, Esc, Space, F, F11…, text entry with the tablet's keyboard | `/dev/uinput`: works on X11 **and** Wayland |
| **Launchers** | Configurable buttons: websites and installed applications (`/admin` page) | browser, `gio launch` |
| **System** | Lock, sleep, restart, shut down (1 s long press): sensitive operations, kept apart from launchers | logind, `org.freedesktop.ScreenSaver` |

State is pushed in real time over WebSocket (`PropertiesChanged`, `Seeked`, `pactl subscribe`); the playback position is interpolated on the tablet.

## Architecture

```
Tablet (fullscreen browser)
        │  HTTP + WebSocket, access token
        ▼
mediadeck (aiohttp, systemd --user service)
   ├─ mpris.py   → org.mpris.MediaPlayer2.*          (session bus)
   ├─ audio.py   → pactl list/set-sink-*, subscribe
   ├─ input.py   → python-evdev / uinput             (virtual keyboard + pointer)
   ├─ keymap.py / text.py → text entry following the PC's real keyboard layout (libxkbcommon)
   └─ system.py  → org.freedesktop.login1, ScreenSaver, launchers (SQLite database, /admin page)
```

KDE Connect is not required; it can stay installed for its other features.

## Installation (Debian / LMDE)

```bash
make deps             # system packages (sudo): venv, gcc, pactl, qrencode
make install          # .venv + Python dependencies
make config           # ~/.config/mediadeck/config.toml (mode 600) with a random token
make uinput           # "uaccess" udev rule for /dev/uinput (sudo)
make service-install  # systemd user service, started with the graphical session
make url              # URL + QR code to scan with the tablet
```

On the tablet: scan the QR code, then browser menu → "Add to Home screen".
The token is stored, then removed from the address bar.

The service must run **inside the graphical session**: it needs the user's session D-Bus and audio server.

## Configuration

`~/.config/mediadeck/config.toml` (see [config.example.toml](config.example.toml)):

```toml
[server]
host = "0.0.0.0"
port = 8765
token = "…"            # make token to regenerate it
advertise_host = "192.168.1.104"   # optional: address used in the QR code (auto-detected otherwise)
tls_cert = "~/.config/mediadeck/tls/server.pem"       # written by `make cert` (HTTPS)
tls_key = "~/.config/mediadeck/tls/server.key"
tls_ca = "~/.config/mediadeck/tls/mediadeck-ca.crt"   # served on /ca.crt for the tablet
http_port = 8766       # optional: plain HTTP port next to HTTPS (0 or absent = disabled)

[audio]
max_volume = 1.0       # cap applied to the tablet

[features]
input = true
power = true
text_input = false       # virtual keyboard: read "Text entry" before enabling

[admin]
enabled = true                 # /admin page and launcher API
allow_custom_command = false   # true: allow raw commands from /admin

# Imported once into the database at first start, then managed on /admin
[[launchers]]
id = "youtube"
label = "YouTube TV"
icon = "tv"            # app, tv, film, globe, music, screen-off, speaker, lock, window
command = ["chromium", "--start-fullscreen", "https://www.youtube.com/tv"]
```

Launcher commands are argv lists and are never passed to a shell.

## Launchers

The buttons of the **Launchers** tab are stored in a SQLite database (`~/.local/share/mediadeck/launchers.db`,
mode 600). The tab's **Manage** button opens the `/admin` page: add, edit, delete and reorder launchers.
Changes show up on the tablet immediately.

| Type | Definition | What the client can write |
|---|---|---|
| **Website** | http(s) URL, browser (Chromium, Chrome, Brave, Firefox), fullscreen | a URL |
| **Installed application** | picked from the list of `.desktop` files, started with `gio launch` | nothing (pick from a list) |
| **Command** | raw argument list | anything: disabled by default |

- **Security**: the API requires the token in the `Authorization: Bearer` header (never in the URL). A command
  runs with your rights: unless `allow_custom_command = true` is set in the `[admin]` section of `config.toml`,
  creating or changing a command is refused (403). An existing command can still be renamed.
- **Migration**: at first start, the `[[launchers]]` of `config.toml` are imported once (command type). They are
  ignored afterwards and can be removed from the file. To import them again, delete the database.
- `enabled = false` turns off the page and the API; existing launchers keep working.
- There is no automatic fullscreen with Firefox, whose kiosk mode ignores F11.
- `gio` (package `libglib2.0-bin`, installed by `make deps`) starts installed applications.

## Text entry

The **Keyboard** button of the Touchpad tab opens an entry window. You type with the tablet's own keyboard (accents,
correction, voice dictation), then **Send** or **Send ↵** types the text into the PC's active window. Tab, Backspace
and Enter sit next to the field. The window is not modal: the touchpad stays usable (click into a field on the PC,
then type). **Hide text** shows dots, for a password.

Each character is translated into key presses following the PC keyboard's **real** layout (AZERTY, QWERTY…), read from
KDE's configuration (`~/.config/kxkbrc`, otherwise `setxkbmap -query`, the active layout coming from D-Bus), using
libxkbcommon. Shift, AltGr and dead keys (ê, ñ…) are handled; keys go through uinput like a real keyboard's.
**The clipboard is not used**: nothing stays in Klipper's history, passwords included.

- **Off by default**: set `[features] text_input = true` to enable it. It is a full keyboard: with the token, someone
  can open a terminal (Super key) and type commands. Enable it only if that is acceptable.
- 500 characters per send (WebSocket message size limit).
- A character missing from the layout (→, emoji…) is skipped, the rest is typed and an error reports it. Typographic
  apostrophes, quotes, "…" and dashes are replaced by their ASCII equivalent.
- The PC's Caps Lock state is not detected: if it is on, capitals are inverted.
- Accented letters made with dead keys rely on the application's compose handling (checked with an xkb simulation, not
  in every application). An input method running on the PC (fcitx, ibus) may change the result.
- Needs libxkbcommon and the xkb data (packages `libxkbcommon0` and `xkb-data`, installed by `make deps`).

## QR codes page

`make qr` opens a page in the PC browser that shows, large, the codes to scan with the tablet: **secure** access
(HTTPS), **unsecured** access (HTTP, if `http_port` is enabled) and the **CA certificate** to install. Only the
codes relevant to the current configuration are shown. The language follows `LANG`.

The codes contain the token, so the page is **never served by the server** (any client on the network could read
it). It is written with mode 600 to `$XDG_RUNTIME_DIR/mediadeck/access.html` (cleared at logout) and opened as
`file://`. It relies on `qrencode` (installed by `make deps`) and needs no script or external resource.

## Local HTTPS

Some mobile browsers require HTTPS, and so do the Wake Lock API and a proper PWA install.
MediaDeck creates its own local certificate authority (CA) with `openssl`:

```bash
make cert                              # CA + server certificate, enables HTTPS in config.toml
systemctl --user restart mediadeck     # or rerun `make dev`
```

On the tablet, **once**:

1. Open `https://<ip>:8765/ca.crt` (address and QR code printed by `make cert`) and accept the certificate
   warning: the file `mediadeck-ca.crt` is downloaded.
2. Settings → Security → Encryption & credentials → Install a certificate → **CA certificate**, then pick that
   file (the exact path depends on the Android version).
3. Rescan the QR code from `make url`: the address now starts with `https://`, so the token remembered for the
   old `http://` address is no longer used.

Notes:

- The port now speaks HTTPS only: a plain `http://` request to it fails. To keep plain HTTP as well, see
  "Choosing: secure or unsecured" below.
- The CA can only sign private addresses (10/8, 172.16/12, 192.168/16, 127/8) and the names `localhost`,
  `*.local`, `*.lan` and the machine's own name (`nameConstraints`). Its key `ca.key` stays on the PC (mode 600):
  never copy it.
- The server certificate is valid for 825 days. `make cert` reissues it without changing the CA, so nothing
  needs redoing on the tablet. If the PC's address changes, fix `advertise_host` and rerun `make cert`.
- Tablet browser: use a standalone Chromium browser (Chrome, Brave, Vivaldi, Vanadium…), which trusts
  user-installed CAs. Browsers built on the system WebView (DuckDuckGo…) may ignore them, and Firefox for Android
  needs a hidden setting: not verified for this project. Only installing the CA is system-wide; `/ca.crt` can be
  downloaded from any browser.
- To start over (new CA, to be reinstalled on the tablet): delete `~/.config/mediadeck/tls/` and the three
  `tls_*` lines from `config.toml`, then run `make cert`.

### Choosing: secure or unsecured

Two ways in, chosen by the tablet, which can coexist:

| Mode | Address | Good for | Limits |
|---|---|---|---|
| **Unsecured** (HTTP) | `http://<ip>:<port>` | nothing to install; works with every browser | traffic and token in clear text on the LAN; no Wake Lock or PWA |
| **Secure** (HTTPS) | `https://<ip>:<port>` | encrypted traffic, Wake Lock, PWA | the CA must be installed on the tablet (see above) |

| Configuration (`[server]`) | Result |
|---|---|
| no `tls_*` and no `http_port` | plain HTTP only, on `port` (default) |
| `tls_*` (`make cert`) | HTTPS only, on `port` |
| `tls_*` + `http_port = 8766` | HTTPS on `port` **and** plain HTTP on `http_port` |

To offer both: `make cert`, then `make http-on` (writes `http_port = 8766`; `make http-on HTTP_PORT=9000` for
another port), open that port if needed (`make firewall HTTP_PORT=8766`), then restart the service
(`systemctl --user restart mediadeck`, or rerun `make run`). `make url` then prints **two addresses and two QR
codes**, one "Secure", one "Unsecured". Each address is a distinct origin: the token is remembered separately for
each, and a PWA can only be installed from the HTTPS one.

To remove plain HTTP: `make http-off` (HTTPS only). To go back to plain HTTP only, also remove the three `tls_*`
lines from `config.toml` (the certificates stay in `~/.config/mediadeck/tls/`). `/ca.crt` is served on both ports, so the CA can be fetched from the HTTP port
without a certificate warning.

## Make targets

| Target | Purpose |
|---|---|
| `make run` / `make dev` | run in the foreground (normal / verbose) |
| `make check` | validate the configuration |
| `make token` | regenerate the token (tablets must rescan) |
| `make qr` | open a page in the PC browser showing the QR codes to scan (HTTPS, HTTP, CA) |
| `make cert` | create the local CA and HTTPS certificate, enable HTTPS |
| `make http-on` / `make http-off` | add / remove the unsecured plain HTTP port next to HTTPS |
| `make status` / `make logs` | service status and journal |
| `make firewall` | open the port to private networks with ufw (`HTTP_PORT=8766` for the plain HTTP port) |
| `make lint` / `make test` | pyright / unit tests |
| `make service-uninstall`, `make uinput-remove`, `make distclean` | uninstall |

## Security

- `/ws` and `/art` require the token (constant-time comparison); only the HTML page is public.
- The config file is rejected if it is readable by group or others.
- `/art` only serves the `file://` cover of the requested player, and only images under 10 MB.
- The launcher API `/api/*` requires the token in the `Authorization` header (no CSRF possible from another site); raw commands are refused there unless `allow_custom_command = true` is set in the config file.
- WebSocket messages are capped at 4 KB; mouse moves and scrolls are bounded.
- uinput access is granted through a logind ACL (`uaccess`) to the active session user, not through the `input` group, which would allow reading every keyboard.
- Without `make cert`, or on the `http_port` port, traffic is plain HTTP (token included) and meant for a trusted LAN only. With HTTPS it is encrypted, but the port must still not be exposed outside the LAN: for remote access, go through WireGuard. `http_port` is disabled by default.

## Known limitations

- **Screen always on**: the Wake Lock API requires HTTPS (see "Local HTTPS"). Over HTTP, adjust the tablet's sleep setting or use a kiosk browser (Fully Kiosk Browser…).
- **VPN on the tablet**: an active VPN on the client device cuts off the local network, and the page times out ("site can't be reached, took too long to respond"). Allow LAN connections in the VPN app (Proton VPN: Settings → Connection → Advanced settings → LAN connections), turn off Android's lockdown / kill switch, or disconnect the VPN.
- **Text entry**: see its section; off by default, and limited to the characters the PC keyboard layout can produce.
- **Lock / power**: depend on polkit rules. An active local session is allowed by default on Debian.
- Some players (browsers especially) expose no volume, shuffle or repeat: the matching controls are then hidden.

## License

MIT

"""Page HTML locale qui affiche les QR codes d'accès, à scanner avec la tablette.

Les codes contiennent le jeton : la page n'est jamais servie par le serveur (n'importe qui sur le réseau
pourrait la lire). Elle est écrite en mode 600 dans le répertoire d'exécution de l'utilisateur (tmpfs,
vidé à la déconnexion) puis ouverte dans le navigateur du PC.
"""

from __future__ import annotations

import base64
import html
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .config import ConfigError

PAGE_NAME = "access.html"

# Textes de la page (kind -> (titre, aide)) ; la langue suit LC_ALL / LC_MESSAGES / LANG
MESSAGES: dict[str, dict[str, str | tuple[str, str]]] = {
    "fr": {
        "title": "MediaDeck : connecter la tablette",
        "intro": "Scannez un code avec l'appareil photo de la tablette, puis ouvrez le lien proposé.",
        "warning": "Ces codes contiennent le jeton d'accès : ne les partagez pas et ne les capturez pas.",
        "https": (
            "Sécurisé (HTTPS)",
            "Trafic chiffré, écran toujours allumé, installation comme application. "
            "Demande d'avoir installé le certificat de la CA sur la tablette.",
        ),
        "http": (
            "Non sécurisé (HTTP)",
            "Rien à installer. Le trafic et le jeton circulent en clair : réseau local de confiance uniquement.",
        ),
        "ca": (
            "Certificat de la CA",
            "À installer une seule fois sur la tablette pour qu'elle fasse confiance au HTTPS (voir le README).",
        ),
    },
    "en": {
        "title": "MediaDeck: connect the tablet",
        "intro": "Scan a code with the tablet's camera, then open the link it offers.",
        "warning": "These codes contain the access token: do not share or screenshot them.",
        "https": (
            "Secure (HTTPS)",
            "Encrypted traffic, screen kept awake, installable as an app. "
            "Needs the CA certificate to be installed on the tablet.",
        ),
        "http": (
            "Unsecured (HTTP)",
            "Nothing to install. Traffic and token travel in clear text: trusted local network only.",
        ),
        "ca": (
            "CA certificate",
            "Install it once on the tablet so that it trusts HTTPS (see the README).",
        ),
    },
}

PAGE = Template("""<!doctype html>
<html lang="$lang">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="referrer" content="no-referrer">
    <title>$title</title>
    <style>
        :root {
            color-scheme: light dark;
            --bg: #f4f5f7;
            --card: #ffffff;
            --text: #1d2330;
            --muted: #5b6475;
            --accent: #b4540a;
            --border: #d9dde5;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --bg: #14171f;
                --card: #1d222d;
                --text: #e8eaf0;
                --muted: #9aa3b5;
                --accent: #f0a050;
                --border: #323a4a;
            }
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 32px 16px;
            background: var(--bg);
            color: var(--text);
            font: 16px/1.5 system-ui, sans-serif;
        }
        main { max-width: 960px; margin: 0 auto; }
        h1 { margin: 0 0 4px; font-size: 1.6rem; }
        .intro { margin: 0 0 24px; color: var(--muted); }
        .cards {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 16px;
        }
        .card {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            padding: 20px 16px;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            text-align: center;
        }
        .card h2 { margin: 0; font-size: 1.15rem; }
        /* Fond blanc fixe : zone de silence nécessaire à la lecture, quel que soit le thème */
        .qr {
            width: min(100%, 280px);
            aspect-ratio: 1;
            padding: 12px;
            background: #ffffff;
            border-radius: 8px;
            image-rendering: crisp-edges;
        }
        .address { margin: 0; font: 0.85rem ui-monospace, monospace; overflow-wrap: anywhere; }
        .hint { margin: 0; color: var(--muted); font-size: 0.9rem; }
        .warning { margin: 24px 0 0; color: var(--accent); font-weight: 600; }
    </style>
</head>
<body>
    <main>
        <h1>$title</h1>
        <p class="intro">$intro</p>
        <div class="cards">
$cards
        </div>
        <p class="warning">$warning</p>
    </main>
</body>
</html>
""")

CARD = Template("""            <section class="card">
                <h2>$title</h2>
                <img class="qr" alt="QR code" src="data:image/svg+xml;base64,$image">
                <p class="address">$address</p>
                <p class="hint">$hint</p>
            </section>""")


@dataclass(frozen=True)
class QrCard:
    """Un code à afficher : `url` est encodée dans le QR code, `address` (sans jeton) est affichée."""

    kind: str  # "https", "http" ou "ca"
    address: str
    url: str


def detect_language() -> str:
    locale = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "fr" if locale.lower().startswith("fr") else "en"


def qr_svg(url: str) -> bytes:
    """QR code au format SVG (qrencode)."""
    qrencode = shutil.which("qrencode")
    if qrencode is None:
        raise ConfigError("qrencode not found (apt install qrencode)")
    try:
        result = subprocess.run([qrencode, "-t", "SVG", "-m", "2", "-o", "-", url], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"qrencode failed: {exc.stderr.decode(errors='replace').strip()}") from exc
    # Sans anti-aliasing : sinon de fines lignes claires apparaissent entre les modules une fois l'image mise à l'échelle
    return result.stdout.replace(b"<svg ", b'<svg shape-rendering="crispEdges" ', 1)


def build_page(cards: list[QrCard], lang: str | None = None) -> str:
    """HTML autonome (images en data: URI, aucune ressource externe)."""
    text = MESSAGES[lang or detect_language()]
    items = []
    for card in cards:
        title, hint = text[card.kind]
        items.append(
            CARD.substitute(
                title=html.escape(title),
                image=base64.b64encode(qr_svg(card.url)).decode("ascii"),
                address=html.escape(card.address),
                hint=html.escape(hint),
            )
        )
    return PAGE.substitute(
        lang=lang or detect_language(),
        title=html.escape(str(text["title"])),
        intro=html.escape(str(text["intro"])),
        warning=html.escape(str(text["warning"])),
        cards="\n".join(items),
    )


def page_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    directory = Path(runtime) / "mediadeck" if runtime else Path(tempfile.mkdtemp(prefix="mediadeck-"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / PAGE_NAME


def write_page(cards: list[QrCard], lang: str | None = None) -> Path:
    """Écrit la page (mode 600 dès la création : elle contient le jeton) et renvoie son chemin."""
    content = build_page(cards, lang)
    path = page_path()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)  # le fichier existait peut-être déjà avec un autre mode
    return path

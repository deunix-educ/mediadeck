"""Lanceurs gérés en base SQLite : stockage, validation et construction de la commande.

Trois types de lanceurs, du plus sûr au plus ouvert :
- ``web``     : URL http(s) ouverte dans un navigateur choisi parmi une liste fermée ;
- ``app``     : application installée, désignée par son fichier .desktop (lancée via ``gio launch``) ;
- ``command`` : liste argv brute, réservée à la configuration (``admin.allow_custom_command``).
Le client n'écrit donc jamais de commande, sauf autorisation explicite dans le fichier de configuration.

Trois lanceurs spéciaux, un par type de média (``audio``, ``image``, ``video``), ouvrent les fichiers de l'onglet
Médias : le chemin du fichier est ajouté en dernier argument. Ils obéissent aux mêmes règles : ``default``
(application par défaut du système, ``gio open``), ``app`` (fichier .desktop) ou ``command`` (réservée à la
configuration). Le chemin ne vient jamais du client tel quel : il est résolu par ``media.MediaLibrary``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Launcher as ConfigLauncher

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "mediadeck"
DB_PATH = DATA_DIR / "launchers.db"

# Icônes du sprite de l'interface (index.html)
ICONS = ("app", "tv", "film", "globe", "music", "screen-off", "speaker", "lock", "window")
KINDS = ("web", "app", "command")
MEDIA_TYPES = ("audio", "image", "video")
OPENER_KINDS = ("default", "app", "command")

MAX_LAUNCHERS = 64
MAX_LABEL = 40
MAX_URL = 2000
MAX_ARGS = 32
MAX_ARG_LENGTH = 1000

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DESKTOP_ID_RE = re.compile(r"^[A-Za-z0-9._+-]{1,200}\.desktop$")
_URL_FORBIDDEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class LauncherError(ValueError):
    """Requête de lanceur invalide (message lisible par l'utilisateur)."""


class LauncherNotFound(LauncherError):
    """Identifiant de lanceur inconnu."""


class CommandNotAllowed(LauncherError):
    """Commande brute refusée : réservée au fichier de configuration."""


@dataclass(frozen=True)
class Browser:
    id: str
    name: str
    executable: str
    args: tuple[str, ...]
    # None = plein écran automatique impossible (le mode kiosque de Firefox ignore F11)
    fullscreen_args: tuple[str, ...] | None

    @property
    def installed(self) -> bool:
        return shutil.which(self.executable) is not None


BROWSERS = (
    Browser("chromium", "Chromium", "chromium", (), ("--start-fullscreen",)),
    Browser("chrome", "Google Chrome", "google-chrome-stable", (), ("--start-fullscreen",)),
    Browser("brave", "Brave", "brave-browser", (), ("--start-fullscreen",)),
    Browser("firefox", "Firefox", "firefox", ("--new-window",), None),
)


def browser_by_id(browser_id: str) -> Browser | None:
    return next((browser for browser in BROWSERS if browser.id == browser_id), None)


@dataclass(frozen=True)
class LauncherEntry:
    id: str
    label: str
    icon: str
    kind: str
    spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "icon": self.icon, "kind": self.kind, "spec": self.spec}


# ----------------------------------------------------------------------
# Applications installées (.desktop)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DesktopApp:
    id: str
    name: str


def desktop_dirs() -> list[Path]:
    """Dossiers « applications » par ordre de priorité XDG, y compris les exports Flatpak."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    roots = [
        data_home,
        *(Path(entry) for entry in data_dirs.split(":") if entry),
        data_home / "flatpak" / "exports" / "share",
        Path("/var/lib/flatpak/exports/share"),
    ]
    return list(dict.fromkeys(root / "applications" for root in roots))


def _read_desktop_name(path: Path) -> str | None:
    """Nom affichable d'une entrée .desktop lançable, ou None (masquée, terminal, illisible)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    in_entry = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
        elif in_entry and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.strip())
    if values.get("Type") != "Application" or not values.get("Exec") or not values.get("Name"):
        return None
    if any(values.get(flag, "").lower() == "true" for flag in ("NoDisplay", "Hidden", "Terminal")):
        return None
    return values["Name"]


def list_desktop_apps() -> list[DesktopApp]:
    apps: dict[str, DesktopApp] = {}
    for directory in desktop_dirs():
        if not directory.is_dir():
            continue
        for path in directory.glob("*.desktop"):
            # Le premier dossier gagne, comme dans la spécification XDG
            if path.name in apps:
                continue
            name = _read_desktop_name(path)
            if name is not None:
                apps[path.name] = DesktopApp(path.name, name)
    return sorted(apps.values(), key=lambda app: app.name.casefold())


def find_desktop_entry(desktop_id: str) -> Path | None:
    if not _DESKTOP_ID_RE.match(desktop_id):
        return None
    for directory in desktop_dirs():
        path = directory / desktop_id
        if path.is_file() and _read_desktop_name(path) is not None:
            return path
    return None


# ----------------------------------------------------------------------
# Validation et construction de la commande
# ----------------------------------------------------------------------


def _validate_spec(kind: Any, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise LauncherError("spec must be an object")
    match kind:
        case "web":
            url = spec.get("url")
            if not isinstance(url, str) or not 0 < len(url.strip()) <= MAX_URL:
                raise LauncherError("a URL is required")
            url = url.strip()
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc or _URL_FORBIDDEN_RE.search(url):
                raise LauncherError("the URL must be a valid http:// or https:// address")
            browser = browser_by_id(str(spec.get("browser", "")))
            if browser is None:
                raise LauncherError("unknown browser")
            fullscreen = spec.get("fullscreen", False)
            if not isinstance(fullscreen, bool):
                raise LauncherError("fullscreen must be a boolean")
            return {"url": url, "browser": browser.id, "fullscreen": fullscreen and browser.fullscreen_args is not None}
        case "app":
            desktop_id = spec.get("desktop_id")
            if not isinstance(desktop_id, str) or find_desktop_entry(desktop_id) is None:
                raise LauncherError("unknown application")
            return {"desktop_id": desktop_id}
        case "command":
            argv = spec.get("argv")
            if (
                not isinstance(argv, list)
                or not 0 < len(argv) <= MAX_ARGS
                or not all(isinstance(arg, str) and 0 < len(arg) <= MAX_ARG_LENGTH and "\0" not in arg for arg in argv)
            ):
                raise LauncherError(f"argv must be a list of 1 to {MAX_ARGS} non-empty strings")
            return {"argv": list(argv)}
        case _:
            raise LauncherError(f"kind must be one of: {', '.join(KINDS)}")


def validate(
    data: Any, *, allow_command: bool, current: LauncherEntry | None = None
) -> tuple[str, str, str, dict[str, Any]]:
    """Valide une définition de lanceur ; renvoie (label, icon, kind, spec) normalisés."""
    if not isinstance(data, dict):
        raise LauncherError("body must be a JSON object")
    label = data.get("label")
    if not isinstance(label, str) or not 0 < len(label.strip()) <= MAX_LABEL:
        raise LauncherError(f"label must be 1 to {MAX_LABEL} characters")
    icon = data.get("icon", "app")
    if icon not in ICONS:
        raise LauncherError(f"icon must be one of: {', '.join(ICONS)}")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise LauncherError(f"kind must be one of: {', '.join(KINDS)}")
    spec = _validate_spec(kind, data.get("spec"))
    # Une commande déjà enregistrée peut garder sa commande (renommage, icône) sans l'autorisation
    unchanged = current is not None and current.kind == "command" and current.spec == spec
    if kind == "command" and not allow_command and not unchanged:
        raise CommandNotAllowed("custom commands are disabled (admin.allow_custom_command in config.toml)")
    return label.strip(), icon, kind, spec


@dataclass(frozen=True)
class OpenerEntry:
    """Lanceur spécial d'un type de média : ouvre un fichier dont le chemin est ajouté à la commande."""

    type: str
    kind: str
    spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "kind": self.kind, "spec": self.spec}


def validate_opener(
    media_type: Any, data: Any, *, allow_command: bool, current: OpenerEntry | None = None
) -> tuple[str, str, dict[str, Any]]:
    """Valide la définition du lecteur d'un type de média ; renvoie (type, kind, spec) normalisés."""
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
        raise LauncherNotFound(f"media type must be one of: {', '.join(MEDIA_TYPES)}")
    if not isinstance(data, dict):
        raise LauncherError("body must be a JSON object")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in OPENER_KINDS:
        raise LauncherError(f"kind must be one of: {', '.join(OPENER_KINDS)}")
    spec = {} if kind == "default" else _validate_spec(kind, data.get("spec"))
    unchanged = current is not None and current.kind == "command" and current.spec == spec
    if kind == "command" and not allow_command and not unchanged:
        raise CommandNotAllowed("custom commands are disabled (admin.allow_custom_command in config.toml)")
    return media_type, kind, spec


def build_open_argv(opener: OpenerEntry, path: Path) -> list[str]:
    """Commande qui ouvre ``path`` (chemin absolu déjà résolu : il ne peut pas être pris pour une option)."""
    match opener.kind:
        case "default":
            if shutil.which("gio") is None:
                raise LauncherError("gio not found (apt install libglib2.0-bin)")
            return ["gio", "open", str(path)]
        case "app":
            entry = find_desktop_entry(opener.spec.get("desktop_id", ""))
            if entry is None:
                raise LauncherError(f"application not found: {opener.spec.get('desktop_id')}")
            if shutil.which("gio") is None:
                raise LauncherError("gio not found (apt install libglib2.0-bin)")
            return ["gio", "launch", str(entry), str(path)]
        case "command":
            return [*opener.spec["argv"], str(path)]
        case _:
            raise LauncherError(f"unknown opener kind '{opener.kind}'")


def build_argv(entry: LauncherEntry) -> list[str]:
    """Commande à exécuter (liste argv, jamais de shell)."""
    match entry.kind:
        case "web":
            browser = browser_by_id(entry.spec.get("browser", ""))
            if browser is None or not browser.installed:
                raise LauncherError(f"browser not installed: {entry.spec.get('browser')}")
            flags = browser.fullscreen_args if entry.spec.get("fullscreen") and browser.fullscreen_args else ()
            return [browser.executable, *browser.args, *flags, entry.spec["url"]]
        case "app":
            path = find_desktop_entry(entry.spec.get("desktop_id", ""))
            if path is None:
                raise LauncherError(f"application not found: {entry.spec.get('desktop_id')}")
            if shutil.which("gio") is None:
                raise LauncherError("gio not found (apt install libglib2.0-bin)")
            return ["gio", "launch", str(path)]
        case "command":
            return list(entry.spec["argv"])
        case _:
            raise LauncherError(f"unknown launcher kind '{entry.kind}'")


# ----------------------------------------------------------------------
# Stockage
# ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS launchers (
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    icon TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('web', 'app', 'command')),
    spec TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_openers (
    type TEXT PRIMARY KEY CHECK (type IN ('audio', 'image', 'video')),
    kind TEXT NOT NULL CHECK (kind IN ('default', 'app', 'command')),
    spec TEXT NOT NULL
);
"""


def _slug(label: str) -> str:
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_label).strip("-")[:28] or "launcher"


class LauncherStore:
    """Liste ordonnée des lanceurs ; une seule boucle asyncio, donc un seul accès à la fois."""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self._db = sqlite3.connect(path)
        path.chmod(0o600)
        self._db.executescript(_SCHEMA)
        self._db.execute("PRAGMA user_version = 1")
        self._cache: list[LauncherEntry] | None = None

    def close(self) -> None:
        self._db.close()

    def list(self) -> list[LauncherEntry]:
        if self._cache is None:
            rows = self._db.execute("SELECT id, label, icon, kind, spec FROM launchers ORDER BY position").fetchall()
            self._cache = [LauncherEntry(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows]
        return list(self._cache)

    def get(self, launcher_id: str) -> LauncherEntry | None:
        return next((entry for entry in self.list() if entry.id == launcher_id), None)

    def _write(self, sql: str, *params: Any) -> None:
        with self._db:
            self._db.execute(sql, params)
        self._cache = None

    def create(self, label: str, icon: str, kind: str, spec: dict[str, Any]) -> LauncherEntry:
        entries = self.list()
        if len(entries) >= MAX_LAUNCHERS:
            raise LauncherError(f"at most {MAX_LAUNCHERS} launchers")
        taken = {entry.id for entry in entries}
        base = _slug(label)
        launcher_id, counter = base, 1
        while launcher_id in taken:
            counter += 1
            launcher_id = f"{base}-{counter}"
        position = len(entries)
        self._write(
            "INSERT INTO launchers (id, position, label, icon, kind, spec) VALUES (?, ?, ?, ?, ?, ?)",
            launcher_id, position, label, icon, kind, json.dumps(spec),
        )
        return LauncherEntry(launcher_id, label, icon, kind, spec)

    def update(self, launcher_id: str, label: str, icon: str, kind: str, spec: dict[str, Any]) -> LauncherEntry:
        if self.get(launcher_id) is None:
            raise LauncherNotFound("unknown launcher")
        self._write(
            "UPDATE launchers SET label = ?, icon = ?, kind = ?, spec = ? WHERE id = ?",
            label, icon, kind, json.dumps(spec), launcher_id,
        )
        return LauncherEntry(launcher_id, label, icon, kind, spec)

    def delete(self, launcher_id: str) -> None:
        if self.get(launcher_id) is None:
            raise LauncherNotFound("unknown launcher")
        self._write("DELETE FROM launchers WHERE id = ?", launcher_id)

    def reorder(self, ids: list[str]) -> None:
        if sorted(ids) != sorted(entry.id for entry in self.list()):
            raise LauncherError("ids must list every launcher exactly once")
        with self._db:
            self._db.executemany("UPDATE launchers SET position = ? WHERE id = ?", list(enumerate(ids)))
        self._cache = None

    def openers(self) -> dict[str, OpenerEntry]:
        """Lecteur de chaque type de média ; l'application par défaut du système tant qu'aucun n'est choisi."""
        found = {row[0]: row for row in self._db.execute("SELECT type, kind, spec FROM media_openers")}
        return {
            media_type: OpenerEntry(media_type, found[media_type][1], json.loads(found[media_type][2]))
            if media_type in found
            else OpenerEntry(media_type, "default", {})
            for media_type in MEDIA_TYPES
        }

    def set_opener(self, media_type: str, kind: str, spec: dict[str, Any]) -> OpenerEntry:
        with self._db:
            self._db.execute(
                "INSERT INTO media_openers (type, kind, spec) VALUES (?, ?, ?) "
                "ON CONFLICT (type) DO UPDATE SET kind = excluded.kind, spec = excluded.spec",
                (media_type, kind, json.dumps(spec)),
            )
        return OpenerEntry(media_type, kind, spec)

    def seed(self, launchers: Iterable[ConfigLauncher]) -> int:
        """Importe une seule fois les [[launchers]] du fichier de configuration ; renvoie le nombre importé."""
        if self._db.execute("SELECT 1 FROM meta WHERE key = 'seeded'").fetchone() is not None:
            return 0
        imported = 0
        with self._db:
            for position, launcher in enumerate(launchers):
                if not _ID_RE.match(launcher.id):
                    continue
                self._db.execute(
                    "INSERT INTO launchers (id, position, label, icon, kind, spec) VALUES (?, ?, ?, ?, 'command', ?)",
                    (
                        launcher.id,
                        position,
                        launcher.label,
                        launcher.icon if launcher.icon in ICONS else "app",
                        json.dumps({"argv": list(launcher.command)}),
                    ),
                )
                imported += 1
            self._db.execute("INSERT INTO meta (key, value) VALUES ('seeded', '1')")
        self._cache = None
        return imported

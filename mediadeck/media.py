"""Bibliothèque multimédia de l'onglet Médias : dossiers autorisés, parcours et résolution sûre des fichiers.

Le client ne fournit jamais de chemin absolu : il désigne un dossier racine (identifiant) et un chemin relatif.
Le serveur résout ce chemin (liens symboliques compris) et refuse tout ce qui sort de la racine, ainsi que tout
fichier dont l'extension n'est pas un média connu : jamais d'exécutable ni de script.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import Config
from .restapi import bearer_authorized, error_response, json_response

log = logging.getLogger(__name__)

MEDIA_TYPES_EXTENSIONS: dict[str, frozenset[str]] = {
    "audio": frozenset(
        {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".ape", ".mka", ".aiff", ".aif"}
    ),
    "image": frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}),
    # ".ts" est volontairement absent : c'est surtout du TypeScript
    "video": frozenset(
        {
            ".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
            ".wmv", ".flv", ".m2ts", ".mts", ".ogv", ".3gp",
        }
    ),
}
EXTENSION_TYPES: dict[str, str] = {
    extension: media_type for media_type, extensions in MEDIA_TYPES_EXTENSIONS.items() for extension in extensions
}

MAX_ENTRIES = 1000  # entrées renvoyées par dossier
MAX_SCANNED = 20000  # entrées examinées par dossier, pour borner le temps de parcours
MAX_PATH_LENGTH = 1000

_NUMBER_RE = re.compile(r"(\d+)")
_USER_DIR_RE = re.compile(r'^XDG_(MUSIC|VIDEOS|PICTURES)_DIR="(.*)"\s*$')


class MediaError(ValueError):
    """Requête invalide (message lisible par l'utilisateur)."""


class MediaNotFound(MediaError):
    """Dossier racine, dossier ou fichier introuvable."""


@dataclass(frozen=True)
class MediaRoot:
    id: str
    name: str
    path: Path


def user_media_dirs() -> list[Path]:
    """Dossiers Musique, Vidéos et Images de l'utilisateur (XDG), seulement s'ils existent."""
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    found: dict[str, Path] = {}
    try:
        lines = (config_home / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = _USER_DIR_RE.match(line.strip())
        if match:
            found[match.group(1)] = Path(match.group(2).replace("$HOME", str(home)))
    defaults = {"MUSIC": "Music", "VIDEOS": "Videos", "PICTURES": "Pictures"}
    paths = [found.get(key, home / name) for key, name in defaults.items()]
    # XDG remplace un dossier désactivé par $HOME : on ne publie jamais tout le dossier personnel
    return [path for path in dict.fromkeys(paths) if path != home and path.is_dir()]


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:28] or "media"


def _natural_key(name: str) -> list[Any]:
    # « piste 2 » avant « piste 10 » ; le découpage alterne texte / nombre, les comparaisons restent homogènes
    return [int(part) if part.isdigit() else part for part in _NUMBER_RE.split(name.casefold())]


def _inside(base: Path, path: Path) -> bool:
    return path == base or base in path.parents


class MediaLibrary:
    """Dossiers autorisés (configuration) et accès aux fichiers qu'ils contiennent."""

    def __init__(self, roots: Iterable[Path] | None) -> None:
        paths = list(roots) if roots is not None else user_media_dirs()
        self._roots: dict[str, MediaRoot] = {}
        for path in paths:
            base, counter = _slug(path.name), 1
            root_id = base
            while root_id in self._roots:
                counter += 1
                root_id = f"{base}-{counter}"
            self._roots[root_id] = MediaRoot(root_id, path.name or str(path), path)

    @property
    def roots(self) -> list[MediaRoot]:
        return list(self._roots.values())

    def snapshot(self) -> dict[str, Any]:
        return {"roots": [{"id": root.id, "name": root.name} for root in self._roots.values()]}

    def describe(self) -> list[str]:
        """Une ligne par dossier, pour le journal de démarrage."""
        return [f"{root.id}: {root.path}" + ("" if root.path.is_dir() else " (not found)") for root in self.roots]

    @staticmethod
    def _parts(relative: Any) -> list[str]:
        if relative is None or relative == "":
            return []
        if not isinstance(relative, str) or len(relative) > MAX_PATH_LENGTH or "\0" in relative:
            raise MediaError("invalid path")
        parts = relative.split("/")
        # Les entrées cachées (.x) ne sont jamais listées : on ne les ouvre pas non plus, même en connaissant le nom
        if any(part == "" or part.startswith(".") for part in parts):
            raise MediaError("invalid path")
        return parts

    def _resolve(self, root_id: Any, relative: Any) -> tuple[Path, list[str], Path]:
        """Renvoie (racine réelle, composantes, chemin réel) ; refuse ce qui sort de la racine."""
        root = self._roots.get(root_id) if isinstance(root_id, str) else None
        if root is None:
            raise MediaNotFound("unknown media folder")
        parts = self._parts(relative)
        try:
            base = root.path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MediaNotFound("media folder unavailable") from exc
        try:
            real = base.joinpath(*parts).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MediaNotFound("not found") from exc
        if not _inside(base, real):
            raise MediaError("outside of the media folder")
        return base, parts, real

    def list(self, root_id: Any, relative: Any = "") -> dict[str, Any]:
        """Contenu d'un dossier : sous-dossiers puis fichiers médias, triés par ordre naturel (appel bloquant)."""
        base, parts, directory = self._resolve(root_id, relative)
        if not directory.is_dir():
            raise MediaError("not a folder")
        folders: list[tuple[str, str]] = []
        files: list[tuple[str, str]] = []
        scanned = 0
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > MAX_SCANNED:
                        break
                    kind = self._classify(base, entry)
                    if kind == "dir":
                        folders.append((entry.name, kind))
                    elif kind is not None:
                        files.append((entry.name, kind))
        except OSError as exc:
            raise MediaError(f"cannot read the folder: {exc.strerror}") from exc
        ordered = [
            *sorted(folders, key=lambda item: _natural_key(item[0])),
            *sorted(files, key=lambda item: _natural_key(item[0])),
        ]
        return {
            "root": root_id,
            "path": "/".join(parts),
            "parent": "/".join(parts[:-1]) if parts else None,
            "entries": [{"name": name, "kind": kind} for name, kind in ordered[:MAX_ENTRIES]],
            "truncated": len(ordered) > MAX_ENTRIES or scanned > MAX_SCANNED,
        }

    @staticmethod
    def _classify(base: Path, entry: os.DirEntry[str]) -> str | None:
        """« dir », un type de média, ou None si l'entrée ne doit pas être proposée."""
        name = entry.name
        if name.startswith("."):
            return None
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            return None  # nom non décodable : impossible à renvoyer proprement en JSON
        try:
            is_dir = entry.is_dir()
            is_file = not is_dir and entry.is_file()
            if entry.is_symlink():
                # Un lien doit rester dans la racine ; pour un fichier, c'est la cible qui décide du type
                target = Path(entry.path).resolve(strict=True)
                if not _inside(base, target):
                    return None
                return "dir" if is_dir else EXTENSION_TYPES.get(target.suffix.lower()) if is_file else None
        except (OSError, RuntimeError):
            return None
        if is_dir:
            return "dir"
        return EXTENSION_TYPES.get(os.path.splitext(name)[1].lower()) if is_file else None

    def file(self, root_id: Any, relative: Any) -> tuple[Path, str]:
        """Fichier média à ouvrir : (chemin réel, type). Le type vient de l'extension du fichier réel."""
        _base, parts, real = self._resolve(root_id, relative)
        if not parts or not real.is_file():
            raise MediaError("not a file")
        media_type = EXTENSION_TYPES.get(real.suffix.lower())
        if media_type is None:
            raise MediaError("not a media file")
        return real, media_type


class MediaApi:
    """GET /api/media?root=…&path=… : contenu d'un dossier (jeton en en-tête Authorization)."""

    def __init__(self, config: Config, library: MediaLibrary) -> None:
        self._config = config
        self._library = library

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/media", self.browse)

    async def browse(self, request: web.Request) -> web.StreamResponse:
        if not bearer_authorized(request, self._config.token):
            return error_response(401, "invalid or missing token")
        try:
            listing = await asyncio.to_thread(
                self._library.list, request.query.get("root"), request.query.get("path", "")
            )
        except MediaNotFound as exc:
            return error_response(404, str(exc))
        except MediaError as exc:
            return error_response(400, str(exc))
        return json_response(listing)

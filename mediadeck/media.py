"""Bibliothèque multimédia de l'onglet Médias : dossiers autorisés, parcours et résolution sûre des fichiers.

Le client ne fournit jamais de chemin absolu : il désigne un dossier racine (identifiant) et un chemin relatif.
Le serveur résout ce chemin (liens symboliques compris) et refuse tout ce qui sort de la racine, ainsi que tout
fichier dont l'extension n'est pas un média connu : jamais d'exécutable ni de script.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from aiohttp import web

from .config import Config
from .restapi import bearer_authorized, error_response, json_response
from .thumbs import SIZES, Thumbnailer

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

MAX_RESULTS = 300  # résultats d'une recherche
MAX_SEARCH_SCANNED = 200000  # entrées examinées par recherche
SEARCH_SECONDS = 4.0  # durée maximale d'une recherche
MAX_QUERY_LENGTH = 100
MAX_QUERY_WORDS = 8

# Images d'accompagnement d'un fichier son ou vidéo, par ordre de préférence (nom sans extension, en minuscules)
ART_NAMES = ("cover", "folder", "front", "poster", "albumart", "thumb")
ART_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

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


def _fold(text: str) -> str:
    """Minuscules sans accents : « Été » et « ete » se correspondent."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def thumb_url(root_id: str, relative: str, size: int, version: int) -> str:
    """Adresse de la miniature ; le client y ajoute le jeton (comme pour /art). ``version`` : date du fichier."""
    return f"/thumb?root={quote(root_id)}&path={quote(relative)}&size={size}&v={version}"


@dataclass(frozen=True)
class Located:
    """Un fichier média situé dans une racine."""

    root: str
    relative: str
    type: str
    real: Path


class MediaLibrary:
    """Dossiers autorisés (configuration) et accès aux fichiers qu'ils contiennent."""

    def __init__(self, roots: Iterable[Path] | None, thumbnails: bool = False, follow_symlinks: bool = True) -> None:
        paths = list(roots) if roots is not None else user_media_dirs()
        self.thumbnails = thumbnails
        # Faux : un lien n'est suivi que si sa cible reste dans la racine (mode strict)
        self._follow = follow_symlinks
        self._last: dict[str, Any] | None = None
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
        return {
            "roots": [{"id": root.id, "name": root.name} for root in self._roots.values()],
            "thumbnails": self.thumbnails,
            "last": self._last,
        }

    def remember(self, located: Located) -> None:
        """Garde le dernier fichier ouvert (titre et image) pour l'onglet Lecture."""
        self._last = {
            "name": Path(located.relative).stem,
            "type": located.type,
            "art": self._art_url(located),
        }

    def _art_url(self, located: Located, size: int = 640) -> str | None:
        if not self.thumbnails:
            return None
        try:
            version = int(located.real.stat().st_mtime)
        except OSError:
            return None
        return thumb_url(located.root, located.relative, size, version)

    def locate(self, path: Path) -> Located | None:
        """Retrouve, à partir d'un chemin absolu (ex. xesam:url d'un lecteur), le fichier média d'une racine."""
        return self._locate_real(path) or self._locate_through_link(path)

    def _locate_real(self, path: Path) -> Located | None:
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        media_type = EXTENSION_TYPES.get(real.suffix.lower())
        if media_type is None or not real.is_file():
            return None
        for root in self._roots.values():
            try:
                base = root.path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if _inside(base, real) and real != base:
                parts = real.relative_to(base).parts
                if any(part.startswith(".") for part in parts):
                    return None
                return Located(root.id, "/".join(parts), media_type, real)
        return None

    def _locate_through_link(self, path: Path) -> Located | None:
        """Fichier atteint par un lien dont la cible est hors des racines : le lecteur annonce le chemin tel qu'il l'a
        reçu (voir ``open_path``), donc sous la racine."""
        if not self._follow:
            return None
        lexical = Path(os.path.normpath(path))
        for root in self._roots.values():
            try:
                relative = lexical.relative_to(root.path)
            except ValueError:
                continue
            try:
                return self.resolve_file(root.id, "/".join(relative.parts))
            except MediaError:
                return None
        return None

    def enrich(self, metadata: dict[str, Any]) -> None:
        """Complète les métadonnées d'un lecteur MPRIS qui joue un fichier de la bibliothèque : titre et image."""
        url = metadata.get("url")
        if not isinstance(url, str) or not url.startswith("file://"):
            return
        located = self.locate(Path(unquote(urlparse(url).path)))
        if located is None:
            return
        if not metadata.get("title"):
            metadata["title"] = Path(located.relative).stem
        art = self._art_url(located)
        if art is not None and not metadata.get("art"):
            metadata["art"] = art

    def find_art(self, root_id: str, file: Path) -> Path | None:
        """Image qui accompagne un fichier son ou vidéo : « nom.jpg », « nom-poster.jpg », puis cover/folder/poster…
        Cherchée à côté du fichier réel (qui peut être hors de la racine quand le dossier est relié)."""
        if root_id not in self._roots:
            return None
        try:
            base = self._base(root_id)
        except MediaNotFound:
            return None
        stem = file.stem.casefold()
        wanted = [stem, f"{stem}-poster", f"{stem}-thumb", f"{stem}-cover", *ART_NAMES]
        best: tuple[int, Path] | None = None
        try:
            with os.scandir(file.parent) as iterator:
                for entry in iterator:
                    name, extension = os.path.splitext(entry.name.casefold())
                    if extension not in ART_EXTENSIONS or name not in wanted or not entry.is_file():
                        continue
                    rank = wanted.index(name)
                    candidate = Path(entry.path).resolve(strict=True)
                    if (self._follow or _inside(base, candidate)) and (best is None or rank < best[0]):
                        best = (rank, candidate)
        except (OSError, RuntimeError):
            return None
        return best[1] if best else None

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

    def _base(self, root_id: Any) -> Path:
        """Chemin réel d'une racine (déjà validée par ``_resolve``)."""
        try:
            return self._roots[str(root_id)].path.resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise MediaNotFound("media folder unavailable") from exc

    def _resolve(self, root_id: Any, relative: Any) -> tuple[list[str], Path]:
        """Renvoie (composantes, chemin réel). Les liens sont suivis où qu'ils pointent (dossiers rangés sur un autre
        disque et reliés depuis la racine) ; le client n'envoie jamais de chemin absolu ni de composante ``..`` ou
        cachée (voir ``_parts``), donc seuls les liens posés par l'utilisateur dans ses dossiers mènent hors de la racine."""
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
        if not self._follow and not _inside(base, real):
            raise MediaError("outside of the media folder")
        return parts, real

    def list(self, root_id: Any, relative: Any = "") -> dict[str, Any]:
        """Contenu d'un dossier : sous-dossiers puis fichiers médias, triés par ordre naturel (appel bloquant)."""
        parts, directory = self._resolve(root_id, relative)
        if not directory.is_dir():
            raise MediaError("not a folder")
        base = self._base(root_id)
        folders: list[tuple[str, str, int]] = []
        files: list[tuple[str, str, int]] = []
        scanned = 0
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > MAX_SCANNED:
                        break
                    kind = self._classify(base, entry)
                    if kind == "dir":
                        folders.append((entry.name, kind, 0))
                    elif kind is not None:
                        files.append((entry.name, kind, self._version(entry) if kind == "image" else 0))
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
            "entries": [self._entry(name, kind, version) for name, kind, version in ordered[:MAX_ENTRIES]],
            "truncated": len(ordered) > MAX_ENTRIES or scanned > MAX_SCANNED,
        }

    @staticmethod
    def _version(entry: os.DirEntry[str]) -> int:
        """Date du fichier : change l'adresse de la miniature quand l'image est modifiée."""
        try:
            return int(entry.stat().st_mtime)
        except OSError:
            return 0

    @staticmethod
    def _entry(name: str, kind: str, version: int) -> dict[str, Any]:
        return {"name": name, "kind": kind, "v": version} if kind == "image" else {"name": name, "kind": kind}

    def _classify(self, base: Path, entry: os.DirEntry[str]) -> str | None:
        """« dir », un type de média, ou None si l'entrée ne doit pas être proposée. Les liens sont suivis (en mode
        strict, seulement vers l'intérieur de la racine ``base``) ; pour un fichier c'est la cible qui décide du type
        (``lien.mp3`` vers un script n'est jamais proposé)."""
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
            if not is_dir and not is_file:
                return None  # y compris un lien cassé ou une boucle de liens
            if entry.is_symlink():
                target = Path(entry.path).resolve(strict=True)
                if not self._follow and not _inside(base, target):
                    return None
                return "dir" if is_dir else EXTENSION_TYPES.get(target.suffix.lower())
        except (OSError, RuntimeError):
            return None
        return "dir" if is_dir else EXTENSION_TYPES.get(os.path.splitext(name)[1].lower())

    def resolve_file(self, root_id: Any, relative: Any) -> Located:
        """Fichier média désigné par (racine, chemin relatif). Le type vient de l'extension du fichier réel."""
        parts, real = self._resolve(root_id, relative)
        if not parts or not real.is_file():
            raise MediaError("not a file")
        media_type = EXTENSION_TYPES.get(real.suffix.lower())
        if media_type is None:
            raise MediaError("not a media file")
        return Located(str(root_id), "/".join(parts), media_type, real)

    def file(self, root_id: Any, relative: Any) -> tuple[Path, str]:
        """Fichier média à ouvrir : (chemin réel, type)."""
        located = self.resolve_file(root_id, relative)
        return located.real, located.type

    def open_path(self, located: Located) -> Path:
        """Chemin à donner au lecteur : celui de la racine, liens conservés (``Vidéos/Cinema/film.mkv``). Le lecteur
        l'annonce tel quel par MPRIS, ce qui permet de retrouver le fichier (titre, image) ; la cible réelle, elle,
        est hors de toute racine quand le dossier est relié."""
        return self._roots[located.root].path.joinpath(*located.relative.split("/"))

    @staticmethod
    def _search_words(query: Any) -> list[str]:
        if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH:
            raise MediaError("invalid search")
        words = _fold(query).split()
        if sum(len(word) for word in words) < 2 or len(words) > MAX_QUERY_WORDS:
            raise MediaError("search needs 2 to 100 characters")
        return words

    def search(self, query: Any, root_id: Any = None, relative: Any = "") -> dict[str, Any]:
        """Recherche par nom (sans accents ni casse, tous les mots) dans un dossier et ses sous-dossiers, ou dans
        toutes les racines si ``root_id`` est absent. Bornée en résultats, en examens et en durée (appel bloquant)."""
        words = self._search_words(query)
        starts: list[tuple[str, tuple[str, ...], Path]] = []  # (racine, chemin logique de départ, dossier réel)
        if root_id in (None, ""):
            for root in self._roots.values():
                try:
                    starts.append((root.id, (), root.path.resolve(strict=True)))
                except (OSError, RuntimeError):
                    continue
        else:
            parts, directory = self._resolve(root_id, relative)
            if not directory.is_dir():
                raise MediaError("not a folder")
            starts.append((str(root_id), tuple(parts), directory))

        deadline = time.monotonic() + SEARCH_SECONDS
        results: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for rid, first, start in starts:
            base = self._base(rid)
            # Les liens sont suivis ; un dossier déjà parcouru (boucle, second lien vers le même dossier) est ignoré.
            # Les résultats gardent le chemin logique (par le lien), celui que l'interface sait rouvrir.
            visited: set[tuple[int, int]] = set()
            stack = [(first, start)]
            while stack and not truncated:
                logical, directory = stack.pop()
                try:
                    info = os.stat(directory)
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) in visited:
                    continue
                visited.add((info.st_dev, info.st_ino))
                try:
                    with os.scandir(directory) as iterator:
                        for entry in iterator:
                            scanned += 1
                            if scanned > MAX_SEARCH_SCANNED or time.monotonic() > deadline:
                                truncated = True
                                break
                            kind = self._classify(base, entry)
                            if kind is None:
                                continue
                            if kind == "dir" and (self._follow or not entry.is_symlink()):
                                stack.append(((*logical, entry.name), Path(entry.path)))
                            folded = _fold(entry.name)
                            if all(word in folded for word in words):
                                found = self._entry(entry.name, kind, self._version(entry) if kind == "image" else 0)
                                results.append({**found, "root": rid, "folder": "/".join(logical)})
                except OSError:
                    continue  # dossier illisible : on passe au suivant
        results.sort(
            key=lambda item: (
                item["kind"] != "dir",
                _natural_key(item["root"]),
                _natural_key(item["folder"]),
                _natural_key(item["name"]),
            )
        )
        return {
            "query": query,
            "root": None if root_id in (None, "") else root_id,
            "entries": results[:MAX_RESULTS],
            "truncated": truncated or len(results) > MAX_RESULTS,
        }


class MediaApi:
    """GET /api/media?root=…&path=… : contenu d'un dossier (jeton en en-tête Authorization)."""

    def __init__(self, config: Config, library: MediaLibrary, thumbnailer: Thumbnailer | None = None) -> None:
        self._config = config
        self._library = library
        self._thumbnailer = thumbnailer

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/media", self.browse)
        app.router.add_get("/api/media/search", self.search)
        if self._thumbnailer is not None and self._thumbnailer.available:
            app.router.add_get("/thumb", self.thumb)

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

    async def search(self, request: web.Request) -> web.StreamResponse:
        if not bearer_authorized(request, self._config.token):
            return error_response(401, "invalid or missing token")
        try:
            result = await asyncio.to_thread(
                self._library.search,
                request.query.get("q"),
                request.query.get("root"),
                request.query.get("path", ""),
            )
        except MediaNotFound as exc:
            return error_response(404, str(exc))
        except MediaError as exc:
            return error_response(400, str(exc))
        return json_response(result)

    async def thumb(self, request: web.Request) -> web.StreamResponse:
        """Miniature d'un fichier. Le jeton est dans l'URL comme pour /art : une balise <img> n'envoie pas d'en-tête."""
        supplied = request.query.get("token", "")
        if not hmac.compare_digest(supplied.encode(), self._config.token.encode()):
            raise web.HTTPUnauthorized()
        assert self._thumbnailer is not None
        try:
            size = int(request.query.get("size", "0"))
        except ValueError:
            size = 0
        if size not in SIZES:
            raise web.HTTPBadRequest(text=f"size must be one of {', '.join(map(str, SIZES))}")
        try:
            located = await asyncio.to_thread(
                self._library.resolve_file, request.query.get("root"), request.query.get("path")
            )
        except MediaNotFound as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except MediaError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        art = None
        if located.type != "image":
            art = await asyncio.to_thread(self._library.find_art, located.root, located.real)
        image = await self._thumbnailer.thumbnail(located.real, located.type, size, art)
        if image is None:
            raise web.HTTPNotFound(text="no thumbnail")
        return web.FileResponse(
            image, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=86400"}
        )

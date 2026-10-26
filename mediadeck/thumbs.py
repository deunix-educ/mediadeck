"""Miniatures et images d'illustration des médias, fabriquées par ffmpeg et gardées dans un cache disque.

- image : version réduite (carré recadré) du fichier ;
- vidéo : affiche du dossier (poster.jpg, nom.jpg…) sinon une image de la vidéo ;
- son   : pochette intégrée au fichier sinon pochette du dossier (cover.jpg, folder.jpg…).

ffmpeg est optionnel : sans lui, ``available`` est faux et l'interface garde ses icônes. Les fichiers traités sont
déjà validés par ``media.MediaLibrary`` (dans une racine autorisée, extension de média connue). Précautions
supplémentaires : aucune entrée standard, protocoles limités aux fichiers locaux, images lues par le seul
démultiplexeur d'image (jamais une « liste de lecture » déguisée), une seule image, délai maximal, 3 processus au plus.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "mediadeck" / "thumbs"

SIZES = (160, 320, 640)  # côtés autorisés (px) : tuiles, intermédiaire, grande image de l'onglet Lecture
# Formats d'image que ffmpeg décode sans bibliothèque supplémentaire (HEIC, AVIF : l'icône reste)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
TIMEOUT_SECONDS = 20
CONCURRENCY = 3
MAX_FILES = 3000  # au-delà, les plus anciennes miniatures sont supprimées
KEEP_FILES = 2500
PRUNE_EVERY = 50  # nombre de miniatures fabriquées entre deux nettoyages


class Thumbnailer:
    def __init__(self, cache_dir: Path = CACHE_DIR, ffmpeg: str | None = None) -> None:
        self._ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self._dir = cache_dir
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._made = 0

    @property
    def available(self) -> bool:
        return self._ffmpeg is not None

    def _key(self, path: Path, size: int, art: Path | None) -> str:
        parts = [str(path), str(size)]
        for source in (path, art):
            if source is not None:
                stat = source.stat()
                parts += [str(source), str(stat.st_mtime_ns), str(stat.st_size)]
        return hashlib.sha1("\0".join(parts).encode()).hexdigest()[:32]

    async def thumbnail(self, path: Path, media_type: str, size: int, art: Path | None = None) -> Path | None:
        """Miniature carrée (JPEG) du média, ou None s'il n'y a rien à montrer. ``art`` : image du dossier, s'il y en a une."""
        if self._ffmpeg is None or size not in SIZES:
            return None
        try:
            key = await asyncio.to_thread(self._key, path, size, art)
        except OSError:
            return None
        target, missing = self._dir / f"{key}.jpg", self._dir / f"{key}.none"
        if target.is_file():
            os.utime(target)  # utilisée récemment : la dernière à être supprimée par le nettoyage
            return target
        if missing.is_file():
            return None  # déjà essayé sans résultat : on ne relance pas ffmpeg à chaque affichage
        async with self._semaphore:
            if target.is_file():
                return target
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            partial = self._dir / f".{key}.part.jpg"
            made = False
            for kind, source in self._plan(path, media_type, art):
                if await self._extract(kind, source, size, partial):
                    made = True
                    break
            if made:
                os.replace(partial, target)
                self._made += 1
                if self._made % PRUNE_EVERY == 0:
                    await asyncio.to_thread(self._prune)
            else:
                partial.unlink(missing_ok=True)
                missing.touch()
            return target if made else None

    @staticmethod
    def _plan(path: Path, media_type: str, art: Path | None) -> list[tuple[str, Path]]:
        """Sources à essayer dans l'ordre : (méthode, fichier)."""
        picture = [("image", art)] if art is not None else []
        match media_type:
            case "image":
                return [("image", path)]
            case "video":
                return [*picture, ("video", path)]
            case "audio":
                return [("embedded", path), *picture]
            case _:
                return []

    async def _extract(self, kind: str, source: Path, size: int, output: Path) -> bool:
        assert self._ffmpeg is not None
        if kind == "image" and source.suffix.lower() not in IMAGE_EXTENSIONS:
            return False
        scale = (
            f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size},format=yuvj420p"
        )
        head = [self._ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file"]
        tail = ["-frames:v", "1", "-vf", scale, "-q:v", "5", "-update", "1", "-y", str(output)]
        match kind:
            case "image":
                attempts = [[*head, "-f", "image2", "-i", str(source), *tail]]
            case "video":
                # Une image prise après 5 s (hors du générique) ; vidéo plus courte : la première image
                attempts = [[*head, "-ss", offset, "-i", str(source), *tail] for offset in ("5", "0")]
            case "embedded":
                attempts = [[*head, "-i", str(source), "-an", "-map", "0:v:0", *tail]]
            case _:
                return False
        for command in attempts:
            output.unlink(missing_ok=True)
            if await self._run(command) and output.is_file() and output.stat().st_size > 0:
                return True
        return False

    @staticmethod
    async def _run(command: list[str]) -> bool:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(process.wait(), TIMEOUT_SECONDS) == 0
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            log.warning("ffmpeg timed out after %g s: %s", TIMEOUT_SECONDS, command[-3:])
            return False

    def _prune(self) -> None:
        """Supprime les plus anciennes miniatures quand le cache dépasse MAX_FILES fichiers."""
        try:
            dated = [
                (entry.stat().st_mtime, entry)
                for entry in self._dir.iterdir()
                if entry.suffix in (".jpg", ".none") and not entry.name.startswith(".")
            ]
        except OSError:
            return
        if len(dated) <= MAX_FILES:
            return
        dated.sort(key=lambda item: item[0])
        for _modified, entry in dated[: len(dated) - KEEP_FILES]:
            entry.unlink(missing_ok=True)

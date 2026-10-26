"""Tests des miniatures (ffmpeg) : images, vidéos, pochettes, cache, refus et nettoyage. Ignorés sans ffmpeg."""

import asyncio
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediadeck import thumbs
from mediadeck.thumbs import Thumbnailer

FFMPEG = shutil.which("ffmpeg")


def ffmpeg(*args: str) -> bool:
    assert FFMPEG
    return subprocess.run([FFMPEG, "-v", "error", "-y", *args], capture_output=True).returncode == 0


def jpeg_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    index = 2
    while index < len(data):
        marker, length = data[index + 1], struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in (0xC0, 0xC2):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    raise AssertionError("no SOF marker")


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class ThumbnailerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.media = Path(cls._tmp.name) / "media"
        cls.media.mkdir()
        m = cls.media
        assert ffmpeg("-f", "lavfi", "-i", "testsrc=s=1600x1200", "-frames:v", "1", str(m / "photo.jpg"))
        assert ffmpeg("-f", "lavfi", "-i", "testsrc=s=400x900", "-frames:v", "1", str(m / "portrait.png"))
        cls.has_video = ffmpeg("-f", "lavfi", "-i", "testsrc=d=3:s=640x360:r=10", "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(m / "clip.mp4"))
        ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=320x180:r=10", "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(m / "short.mp4"))
        cls.has_cover = ffmpeg(
            "-f", "lavfi", "-i", "sine=d=1", "-i", str(m / "photo.jpg"), "-map", "0:a", "-map", "1:v",
            "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(m / "withcover.flac"),
        )
        assert ffmpeg("-f", "lavfi", "-i", "sine=d=1", "-c:a", "flac", str(m / "nocover.flac"))
        (m / "playlist.jpg").write_text("#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n")
        (m / "photo.heic").write_bytes(b"not decodable here")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self._cache = tempfile.TemporaryDirectory()
        self.cache = Path(self._cache.name) / "thumbs"
        self.thumbnailer = Thumbnailer(self.cache)

    def tearDown(self) -> None:
        self._cache.cleanup()

    async def test_image_thumbnail_is_a_square_jpeg(self) -> None:
        for source, size in (("photo.jpg", 160), ("photo.jpg", 640), ("portrait.png", 320)):
            with self.subTest(source=source, size=size):
                result = await self.thumbnailer.thumbnail(self.media / source, "image", size)
                assert result is not None
                self.assertEqual(jpeg_size(result), (size, size))
        self.assertEqual(self.cache.stat().st_mode & 0o077, 0)

    async def test_cache_avoids_a_second_ffmpeg_run(self) -> None:
        first = await self.thumbnailer.thumbnail(self.media / "photo.jpg", "image", 160)
        with mock.patch("mediadeck.thumbs.asyncio.create_subprocess_exec", side_effect=AssertionError("ffmpeg relancé")):
            again = await self.thumbnailer.thumbnail(self.media / "photo.jpg", "image", 160)
        self.assertEqual(first, again)

    async def test_modified_file_gets_a_new_thumbnail(self) -> None:
        photo = self.media / "edit.jpg"
        shutil.copy(self.media / "photo.jpg", photo)
        first = await self.thumbnailer.thumbnail(photo, "image", 160)
        os.utime(photo, ns=(2_000_000_000, 2_000_000_000))
        second = await self.thumbnailer.thumbnail(photo, "image", 160)
        self.assertNotEqual(first, second)

    async def test_refused_sizes_and_missing_ffmpeg(self) -> None:
        self.assertIsNone(await self.thumbnailer.thumbnail(self.media / "photo.jpg", "image", 100))
        with mock.patch("mediadeck.thumbs.shutil.which", return_value=None):
            unavailable = Thumbnailer(self.cache)
        self.assertFalse(unavailable.available)
        self.assertIsNone(await unavailable.thumbnail(self.media / "photo.jpg", "image", 160))

    async def test_unsupported_and_disguised_files_give_nothing(self) -> None:
        # HEIC : pas de décodeur ; « playlist.jpg » : texte HLS déguisé, jamais interprété comme une liste de lecture
        for name in ("photo.heic", "playlist.jpg"):
            with self.subTest(name=name):
                self.assertIsNone(await self.thumbnailer.thumbnail(self.media / name, "image", 160))

    async def test_failure_is_remembered(self) -> None:
        self.assertIsNone(await self.thumbnailer.thumbnail(self.media / "nocover.flac", "audio", 160))
        self.assertEqual(len(list(self.cache.glob("*.none"))), 1)
        with mock.patch("mediadeck.thumbs.asyncio.create_subprocess_exec", side_effect=AssertionError("ffmpeg relancé")):
            self.assertIsNone(await self.thumbnailer.thumbnail(self.media / "nocover.flac", "audio", 160))

    async def test_embedded_cover_of_a_sound_file(self) -> None:
        if not self.has_cover:
            self.skipTest("cannot build a FLAC with a cover")
        result = await self.thumbnailer.thumbnail(self.media / "withcover.flac", "audio", 320)
        assert result is not None
        self.assertEqual(jpeg_size(result), (320, 320))

    async def test_folder_art_is_the_fallback_for_sound_and_preferred_for_video(self) -> None:
        result = await self.thumbnailer.thumbnail(self.media / "nocover.flac", "audio", 160, art=self.media / "portrait.png")
        assert result is not None
        self.assertEqual(jpeg_size(result), (160, 160))

    async def test_video_frame_even_for_a_short_video(self) -> None:
        if not self.has_video:
            self.skipTest("cannot encode a test video")
        for name in ("clip.mp4", "short.mp4"):  # short.mp4 dure 1 s : le repli sur la première image doit servir
            with self.subTest(name=name):
                result = await self.thumbnailer.thumbnail(self.media / name, "video", 160)
                assert result is not None
                self.assertEqual(jpeg_size(result), (160, 160))

    async def test_timeout_kills_ffmpeg(self) -> None:
        # « sleep <arguments> » ne termine pas dans le délai : le processus doit être tué, sans miniature
        with mock.patch.object(thumbs, "TIMEOUT_SECONDS", 0.05):
            slow = Thumbnailer(self.cache, ffmpeg="/bin/sleep")
            result = await slow.thumbnail(self.media / "photo.jpg", "image", 160)
        self.assertIsNone(result)

    async def test_prune_keeps_the_most_recent_files(self) -> None:
        self.cache.mkdir(parents=True)
        for index in range(8):
            entry = self.cache / f"{index}.jpg"
            entry.write_bytes(b"x")
            os.utime(entry, (1000 + index, 1000 + index))
        (self.cache / ".partiel.part.jpg").write_bytes(b"x")
        with mock.patch.object(thumbs, "MAX_FILES", 5), mock.patch.object(thumbs, "KEEP_FILES", 3):
            self.thumbnailer._prune()
        self.assertEqual(sorted(entry.name for entry in self.cache.glob("[0-9]*.jpg")), ["5.jpg", "6.jpg", "7.jpg"])
        self.assertTrue((self.cache / ".partiel.part.jpg").exists())

    async def test_concurrency_is_bounded(self) -> None:
        running = 0
        peak = 0
        original = Thumbnailer._run

        async def counting(command: list[str]) -> bool:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.05)
                return await original(command)
            finally:
                running -= 1

        shutil.copy(self.media / "photo.jpg", self.media / "a1.jpg")
        files = []
        for index in range(8):
            copy = self.media / f"c{index}.jpg"
            shutil.copy(self.media / "photo.jpg", copy)
            os.utime(copy, ns=(3_000_000_000 + index, 3_000_000_000 + index))
            files.append(copy)
        with mock.patch.object(Thumbnailer, "_run", staticmethod(counting)):
            await asyncio.gather(*(self.thumbnailer.thumbnail(path, "image", 160) for path in files))
        self.assertLessEqual(peak, thumbs.CONCURRENCY)
        self.assertGreater(peak, 1)


if __name__ == "__main__":
    unittest.main()

"""Tests de la bibliothèque multimédia : classification, tri, évasions de dossier (.., liens), API REST."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mediadeck import media
from mediadeck.config import parse_config
from mediadeck.media import MediaApi, MediaError, MediaLibrary, MediaNotFound, user_media_dirs

TOKEN = "m" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class LibraryCase(unittest.TestCase):
    """Un dossier « Musique » dans un répertoire temporaire, et un fichier secret hors de la racine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.root = base / "Musique"
        self.outside = base / "secret"
        touch(self.outside / "private.mp3")
        touch(self.outside / "private.sh")
        self.root.mkdir()
        self.library = MediaLibrary([self.root])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def names(self, listing: dict) -> list[tuple[str, str]]:
        return [(entry["name"], entry["kind"]) for entry in listing["entries"]]


class ListingTest(LibraryCase):
    def test_classifies_by_extension_and_hides_the_rest(self) -> None:
        for name in ("a.mp3", "b.FLAC", "c.jpg", "d.png", "e.mkv", "f.MP4", "g.txt", "h.sh", "i.desktop", ".hidden.mp3", "j.ts"):
            touch(self.root / name)
        (self.root / "Albums").mkdir()
        (self.root / ".cache").mkdir()
        listing = self.library.list("musique", "")
        self.assertEqual(
            self.names(listing),
            [("Albums", "dir"), ("a.mp3", "audio"), ("b.FLAC", "audio"), ("c.jpg", "image"), ("d.png", "image"),
             ("e.mkv", "video"), ("f.MP4", "video")],
        )
        self.assertFalse(listing["truncated"])

    def test_folders_first_then_natural_order(self) -> None:
        for name in ("piste 10.mp3", "piste 2.mp3", "Piste 1.mp3"):
            touch(self.root / name)
        for name in ("CD 10", "CD 2"):
            (self.root / name).mkdir()
        self.assertEqual(
            [name for name, _ in self.names(self.library.list("musique", ""))],
            ["CD 2", "CD 10", "Piste 1.mp3", "piste 2.mp3", "piste 10.mp3"],
        )

    def test_subfolder_and_parent(self) -> None:
        touch(self.root / "Rock" / "Live" / "a.mp3")
        listing = self.library.list("musique", "Rock/Live")
        self.assertEqual((listing["path"], listing["parent"]), ("Rock/Live", "Rock"))
        self.assertEqual(self.library.list("musique", "Rock")["parent"], "")
        self.assertIsNone(self.library.list("musique", "")["parent"])

    def test_truncation(self) -> None:
        for index in range(5):
            touch(self.root / f"{index}.mp3")
        with mock.patch.object(media, "MAX_ENTRIES", 3):
            listing = self.library.list("musique", "")
        self.assertEqual(len(listing["entries"]), 3)
        self.assertTrue(listing["truncated"])

    def test_undecodable_names_are_skipped(self) -> None:
        touch(self.root / "ok.mp3")
        (self.root / os.fsdecode(b"bad-\xff.mp3")).write_bytes(b"x")
        self.assertEqual(self.names(self.library.list("musique", "")), [("ok.mp3", "audio")])

    def test_unavailable_root(self) -> None:
        library = MediaLibrary([Path(self._tmp.name) / "absent"])
        with self.assertRaises(MediaNotFound):
            library.list("absent", "")

    def test_unknown_root(self) -> None:
        for root in ("nope", None, 3, ""):
            with self.subTest(root=root), self.assertRaises(MediaNotFound):
                self.library.list(root, "")


class EscapeTest(LibraryCase):
    """Rien de ce qui est hors de la racine ne doit être listé ni ouvert."""

    def test_path_tricks_are_refused(self) -> None:
        touch(self.root / "a.mp3")
        for path in ("..", "../secret", "a/../..", "/etc", "/etc/passwd", ".", "./a.mp3", "a//b", "a/", "x\0y", "\\..\\x", 5, ["a"]):
            with self.subTest(path=path):
                with self.assertRaises(MediaError):
                    self.library.list("musique", path)
                with self.assertRaises(MediaError):
                    self.library.file("musique", path)

    def test_hidden_entries_are_not_reachable_by_name(self) -> None:
        touch(self.root / ".cache.mp3")
        touch(self.root / ".prive" / "a.mp3")
        for path in (".cache.mp3", ".prive", ".prive/a.mp3"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(MediaError, "invalid path"):
                    self.library.file("musique", path)
                with self.assertRaisesRegex(MediaError, "invalid path"):
                    self.library.list("musique", path)

    def test_overlong_path(self) -> None:
        with self.assertRaises(MediaError):
            self.library.list("musique", "a/" * 600 + "a")

    def test_symlinks_leaving_the_root_are_neither_listed_nor_reachable(self) -> None:
        os.symlink(self.outside, self.root / "dehors")  # dossier
        os.symlink(self.outside / "private.mp3", self.root / "lien.mp3")  # fichier
        touch(self.root / "ok.mp3")
        self.assertEqual(self.names(self.library.list("musique", "")), [("ok.mp3", "audio")])
        for path in ("dehors", "dehors/private.mp3"):
            with self.subTest(path=path), self.assertRaisesRegex(MediaError, "outside"):
                self.library.list("musique", path)
        with self.assertRaisesRegex(MediaError, "outside"):
            self.library.file("musique", "lien.mp3")
        with self.assertRaisesRegex(MediaError, "outside"):
            self.library.file("musique", "dehors/private.mp3")

    def test_symlinks_inside_the_root_are_followed(self) -> None:
        touch(self.root / "Vrai" / "a.mp3")
        os.symlink(self.root / "Vrai", self.root / "Alias")
        os.symlink(self.root / "Vrai" / "a.mp3", self.root / "b.mp3")
        self.assertEqual(
            self.names(self.library.list("musique", "")), [("Alias", "dir"), ("Vrai", "dir"), ("b.mp3", "audio")]
        )
        real, kind = self.library.file("musique", "Alias/a.mp3")
        self.assertEqual((real, kind), (self.root / "Vrai" / "a.mp3", "audio"))

    def test_a_media_name_cannot_hide_a_script(self) -> None:
        # lien.mp3 -> script.sh dans la racine : le type vient du fichier réel, pas du nom du lien
        touch(self.root / "script.sh")
        os.symlink(self.root / "script.sh", self.root / "lien.mp3")
        self.assertEqual(self.names(self.library.list("musique", "")), [])
        with self.assertRaisesRegex(MediaError, "not a media file"):
            self.library.file("musique", "lien.mp3")

    def test_symlink_loop(self) -> None:
        os.symlink("boucle", self.root / "boucle")
        self.assertEqual(self.names(self.library.list("musique", "")), [])
        with self.assertRaises(MediaNotFound):
            self.library.list("musique", "boucle")

    def test_root_itself_may_be_a_symlink(self) -> None:
        touch(self.outside / "b.mp3")
        link = Path(self._tmp.name) / "lien-racine"
        os.symlink(self.outside, link)
        library = MediaLibrary([link])
        self.assertIn(("b.mp3", "audio"), self.names(library.list("lien-racine", "")))


class FileTest(LibraryCase):
    def test_returns_real_path_and_type(self) -> None:
        touch(self.root / "A B" / "été 1.flac")
        real, kind = self.library.file("musique", "A B/été 1.flac")
        self.assertEqual((real, kind), (self.root / "A B" / "été 1.flac", "audio"))

    def test_refuses_folders_roots_unknown_and_non_media(self) -> None:
        touch(self.root / "notes.txt")
        touch(self.root / "run.sh")
        (self.root / "Dossier").mkdir()
        for path, message in (("Dossier", "not a file"), ("", "not a file"), ("notes.txt", "not a media"), ("run.sh", "not a media")):
            with self.subTest(path=path), self.assertRaisesRegex(MediaError, message):
                self.library.file("musique", path)
        with self.assertRaises(MediaNotFound):
            self.library.file("musique", "absent.mp3")


class RootsTest(unittest.TestCase):
    def test_ids_are_unique_slugs(self) -> None:
        library = MediaLibrary([Path("/a/Vidéos"), Path("/b/Vidéos"), Path("/c/Mes Images!")])
        self.assertEqual([root.id for root in library.roots], ["videos", "videos-2", "mes-images"])
        self.assertEqual([root["name"] for root in library.snapshot()["roots"]], ["Vidéos", "Vidéos", "Mes Images!"])

    def test_empty_list_disables_the_library(self) -> None:
        self.assertEqual(MediaLibrary([]).snapshot(), {"roots": []})

    def test_user_dirs_from_xdg_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name in ("Musique", "Films", "Pictures"):
                (home / name).mkdir()
            config = home / ".config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text(
                '# commentaire\nXDG_MUSIC_DIR="$HOME/Musique"\nXDG_VIDEOS_DIR="$HOME/Films"\n'
                'XDG_PICTURES_DIR="$HOME/"\nXDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n'
            )
            with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
                # Images désactivé par XDG (= $HOME) : jamais publié ; Pictures par défaut ne s'applique pas
                self.assertEqual(user_media_dirs(), [home / "Musique", home / "Films"])
            (config / "user-dirs.dirs").unlink()
            with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
                self.assertEqual(user_media_dirs(), [home / "Pictures"])

    def test_default_roots_when_not_configured(self) -> None:
        with mock.patch("mediadeck.media.user_media_dirs", return_value=[Path("/x/Music")]):
            self.assertEqual([root.id for root in MediaLibrary(None).roots], ["music"])


class MediaApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "Videos"
        touch(self.root / "Films" / "a.mkv")
        touch(self.root / "b.mp4")
        config = parse_config({"server": {"token": TOKEN}})
        app = web.Application()
        MediaApi(config, MediaLibrary([self.root])).add_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self._tmp.cleanup()

    async def test_token_required(self) -> None:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}):
            response = await self.client.get("/api/media?root=videos", headers=headers)
            self.assertEqual(response.status, 401)
        # Pas de jeton dans l'URL : il finirait dans les journaux
        response = await self.client.get(f"/api/media?root=videos&token={TOKEN}")
        self.assertEqual(response.status, 401)

    async def test_listing(self) -> None:
        response = await self.client.get("/api/media?root=videos", headers=AUTH)
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual([(e["name"], e["kind"]) for e in data["entries"]], [("Films", "dir"), ("b.mp4", "video")])
        response = await self.client.get("/api/media", params={"root": "videos", "path": "Films"}, headers=AUTH)
        self.assertEqual([e["name"] for e in (await response.json())["entries"]], ["a.mkv"])

    async def test_errors(self) -> None:
        for query, status in (
            ("root=nope", 404), ("", 404), ("root=videos&path=absent", 404),
            ("root=videos&path=..", 400), ("root=videos&path=b.mp4", 400), ("root=videos&path=%2Fetc", 400),
        ):
            response = await self.client.get(f"/api/media?{query}", headers=AUTH)
            self.assertEqual(response.status, status, query)
            self.assertIn("error", await response.json())


if __name__ == "__main__":
    unittest.main()

"""Tests de la bibliothèque multimédia : classification, tri, évasions de dossier (.., liens), API REST."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mediadeck import media
from mediadeck.config import parse_config
from mediadeck.media import Located, MediaApi, MediaError, MediaLibrary, MediaNotFound, thumb_url, user_media_dirs
from mediadeck.thumbs import Thumbnailer

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

    def test_images_carry_their_modification_date_for_thumbnail_caching(self) -> None:
        touch(self.root / "a.jpg")
        touch(self.root / "b.mp3")
        (self.root / "Dossier").mkdir()
        os.utime(self.root / "a.jpg", (1_700_000_000, 1_700_000_000))
        entries = {entry["name"]: entry for entry in self.library.list("musique", "")["entries"]}
        self.assertEqual(entries["a.jpg"], {"name": "a.jpg", "kind": "image", "v": 1_700_000_000})
        self.assertEqual(entries["b.mp3"], {"name": "b.mp3", "kind": "audio"})
        self.assertEqual(entries["Dossier"], {"name": "Dossier", "kind": "dir"})

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
        self.assertEqual(MediaLibrary([]).snapshot(), {"roots": [], "thumbnails": False, "last": None})

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


class SearchTest(LibraryCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("Rock/Live/Été 1980.mp3", "Rock/studio.flac", "Rock/été.txt", "Jazz/Ete indien.mp3", "Films/Été meurtrier.mkv",
                     "Photos/ete-2020.JPG", ".caché/ete.mp3", "Rock/.ete-cache.mp3"):
            touch(self.root / name)
        (self.root / "Été" / "Sous-dossier").mkdir(parents=True)

    def found(self, result: dict) -> list[tuple[str, str, str]]:
        return [(entry["folder"], entry["name"], entry["kind"]) for entry in result["entries"]]

    def test_matches_ignore_case_and_accents_and_need_every_word(self) -> None:
        result = self.library.search("ETE", "musique", "")
        self.assertEqual(
            self.found(result),
            [("", "Été", "dir"), ("Films", "Été meurtrier.mkv", "video"), ("Jazz", "Ete indien.mp3", "audio"),
             ("Photos", "ete-2020.JPG", "image"), ("Rock/Live", "Été 1980.mp3", "audio")],
        )
        self.assertEqual(self.found(self.library.search("été indien", "musique")), [("Jazz", "Ete indien.mp3", "audio")])
        self.assertEqual(self.found(self.library.search("indien 1980", "musique")), [])

    def test_non_media_hidden_and_escaping_entries_are_never_found(self) -> None:
        os.symlink(self.outside, self.root / "dehors-ete")
        os.symlink(self.outside / "private.mp3", self.root / "lien-ete.mp3")
        result = self.found(self.library.search("ete", "musique"))
        names = [name for _folder, name, _kind in result]
        for forbidden in ("été.txt", "ete.mp3", ".ete-cache.mp3", "dehors-ete", "lien-ete.mp3", "private.mp3"):
            self.assertNotIn(forbidden, names)

    def test_search_is_scoped_to_the_folder_and_its_subfolders(self) -> None:
        self.assertEqual(self.found(self.library.search("ete", "musique", "Rock")), [("Rock/Live", "Été 1980.mp3", "audio")])
        with self.assertRaises(MediaError):
            self.library.search("ete", "musique", "Jazz/Ete indien.mp3")
        with self.assertRaises(MediaError):
            self.library.search("ete", "musique", "..")

    def test_all_roots_when_none_is_given(self) -> None:
        other = Path(self._tmp.name).resolve() / "Videos"
        touch(other / "Été au ciné.mp4")
        library = MediaLibrary([self.root, other])
        result = library.search("été")
        self.assertEqual({entry["root"] for entry in result["entries"]}, {"musique", "videos"})
        self.assertIsNone(result["root"])

    def test_image_results_carry_their_version(self) -> None:
        os.utime(self.root / "Photos" / "ete-2020.JPG", (1_700_000_000, 1_700_000_000))
        result = self.library.search("2020", "musique")
        self.assertEqual(result["entries"], [
            {"name": "ete-2020.JPG", "kind": "image", "v": 1_700_000_000, "root": "musique", "folder": "Photos"}])

    def test_symlinked_folders_are_not_followed(self) -> None:
        os.symlink(self.root, self.root / "Rock" / "boucle")  # boucle vers la racine
        result = self.library.search("studio", "musique")
        self.assertEqual(self.found(result), [("Rock", "studio.flac", "audio")])

    def test_invalid_queries(self) -> None:
        for query in (None, 5, "", "a", " é ", "x" * 101, "a b c d e f g h i"):
            with self.subTest(query=query), self.assertRaises(MediaError):
                self.library.search(query, "musique")
        with self.assertRaises(MediaNotFound):
            self.library.search("ete", "nope")

    def test_limits(self) -> None:
        with mock.patch.object(media, "MAX_RESULTS", 2):
            result = self.library.search("ete", "musique")
        self.assertEqual((len(result["entries"]), result["truncated"]), (2, True))
        with mock.patch.object(media, "SEARCH_SECONDS", -1):
            self.assertTrue(self.library.search("ete", "musique")["truncated"])
        with mock.patch.object(media, "MAX_SEARCH_SCANNED", 3):
            self.assertTrue(self.library.search("ete", "musique")["truncated"])


class LocateAndArtTest(LibraryCase):
    def test_locate_a_file_by_its_absolute_path(self) -> None:
        file = touch(self.root / "Rock" / "été.mp3")
        located = self.library.locate(file)
        self.assertEqual(located, Located("musique", "Rock/été.mp3", "audio", file))
        os.symlink(file, self.root / "alias.mp3")
        again = self.library.locate(self.root / "alias.mp3")
        assert again is not None
        self.assertEqual(again.relative, "Rock/été.mp3")

    def test_locate_refuses_everything_else(self) -> None:
        touch(self.root / "notes.txt")
        touch(self.root / ".caché" / "a.mp3")
        for path in (self.outside / "private.mp3", self.root / "notes.txt", self.root / ".caché" / "a.mp3", self.root / "absent.mp3", self.root):
            with self.subTest(path=path):
                self.assertIsNone(self.library.locate(path))

    def test_enrich_adds_a_title_and_an_image_to_a_library_file(self) -> None:
        file = touch(self.root / "Films" / "Mon film (2020).mkv")
        library = MediaLibrary([self.root], thumbnails=True)
        metadata = {"url": file.as_uri(), "title": "", "art": None}
        library.enrich(metadata)
        self.assertEqual(metadata["title"], "Mon film (2020)")
        self.assertTrue(metadata["art"].startswith("/thumb?root=musique&path=Films/Mon%20film%20%282020%29.mkv&size=640&v="))

    def test_enrich_keeps_what_the_player_provides(self) -> None:
        file = touch(self.root / "a.mp3")
        library = MediaLibrary([self.root], thumbnails=True)
        metadata = {"url": file.as_uri(), "title": "Vrai titre", "art": "/art?x"}
        library.enrich(metadata)
        self.assertEqual((metadata["title"], metadata["art"]), ("Vrai titre", "/art?x"))

    def test_enrich_ignores_foreign_urls_and_works_without_thumbnails(self) -> None:
        library = MediaLibrary([self.root], thumbnails=False)
        for url in ("https://example.com/a.mp3", (self.outside / "private.mp3").as_uri(), "file:///absent.mp3", None, 5):
            metadata: dict = {"url": url, "title": ""}
            library.enrich(metadata)
            self.assertEqual(metadata, {"url": url, "title": ""}, url)
        file = touch(self.root / "b.mp3")
        metadata = {"url": file.as_uri(), "title": ""}
        library.enrich(metadata)
        self.assertEqual(metadata["title"], "b")
        self.assertNotIn("art", metadata)

    def test_remember_feeds_the_snapshot(self) -> None:
        file = touch(self.root / "Rock" / "a.mp3")
        library = MediaLibrary([self.root], thumbnails=True)
        self.assertIsNone(library.snapshot()["last"])
        library.remember(library.resolve_file("musique", "Rock/a.mp3"))
        last = library.snapshot()["last"]
        self.assertEqual((last["name"], last["type"]), ("a", "audio"))
        self.assertIn("/thumb?root=musique&path=Rock/a.mp3&size=640", last["art"])
        self.assertTrue(file.exists())

    def test_find_art_prefers_the_file_name_then_generic_names(self) -> None:
        file = touch(self.root / "Film" / "Mon film.mkv")
        self.assertIsNone(self.library.find_art("musique", file))
        touch(self.root / "Film" / "folder.png")
        self.assertEqual(self.library.find_art("musique", file), self.root / "Film" / "folder.png")
        touch(self.root / "Film" / "COVER.JPG")
        self.assertEqual(self.library.find_art("musique", file), self.root / "Film" / "COVER.JPG")
        touch(self.root / "Film" / "Mon film-poster.jpg")
        self.assertEqual(self.library.find_art("musique", file), self.root / "Film" / "Mon film-poster.jpg")
        touch(self.root / "Film" / "Mon film.jpg")
        self.assertEqual(self.library.find_art("musique", file), self.root / "Film" / "Mon film.jpg")
        self.assertIsNone(self.library.find_art("nope", file))

    def test_find_art_never_follows_a_link_out_of_the_root(self) -> None:
        file = touch(self.root / "a.mp3")
        os.symlink(self.outside / "private.mp3", self.root / "cover.jpg")
        self.assertIsNone(self.library.find_art("musique", file))

    def test_thumb_url_is_quoted(self) -> None:
        self.assertEqual(thumb_url("r", "a b/é&c.jpg", 160, 7), "/thumb?root=r&path=a%20b/%C3%A9%26c.jpg&size=160&v=7")


class SearchAndThumbApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "Images"
        touch(self.root / "Vacances" / "plage été.png")
        touch(self.root / "notes.txt")
        self.cache = Path(self._tmp.name) / "cache"
        self.config = parse_config({"server": {"token": TOKEN}})
        self.library = MediaLibrary([self.root], thumbnails=True)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def client(self, thumbnailer: Thumbnailer | None) -> TestClient:
        app = web.Application()
        MediaApi(self.config, self.library, thumbnailer).add_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_search_route(self) -> None:
        client = await self.client(None)
        for headers in ({}, {"Authorization": "Bearer nope"}):
            self.assertEqual((await client.get("/api/media/search?q=ete&root=images", headers=headers)).status, 401)
        self.assertEqual((await client.get(f"/api/media/search?q=ete&token={TOKEN}")).status, 401)
        data = await (await client.get("/api/media/search", params={"q": "ÉTÉ", "root": "images"}, headers=AUTH)).json()
        self.assertEqual([(e["folder"], e["name"]) for e in data["entries"]], [("Vacances", "plage été.png")])
        for query, status in (("q=a&root=images", 400), ("root=images", 400), ("q=ete&root=nope", 404), ("q=ete&root=images&path=..", 400)):
            self.assertEqual((await client.get(f"/api/media/search?{query}", headers=AUTH)).status, status, query)

    async def test_thumb_route_is_only_mounted_with_ffmpeg(self) -> None:
        client = await self.client(None)
        self.assertEqual((await client.get(f"/thumb?root=images&path=x&size=160&token={TOKEN}")).status, 404)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    async def test_thumb_route(self) -> None:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=300x200", "-frames:v", "1",
                        str(self.root / "Vacances" / "plage été.png")], check=True)
        client = await self.client(Thumbnailer(self.cache))
        good = "root=images&path=Vacances/plage%20%C3%A9t%C3%A9.png&size=160"
        self.assertEqual((await client.get(f"/thumb?{good}")).status, 401)
        self.assertEqual((await client.get(f"/thumb?{good}&token=nope")).status, 401)
        self.assertEqual((await client.get(f"/thumb?{good}", headers=AUTH)).status, 401)  # l'en-tête ne suffit pas ici
        response = await client.get(f"/thumb?{good}&token={TOKEN}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=86400")
        self.assertEqual((await response.read())[:2], b"\xff\xd8")
        for query, status in (
            ("root=images&path=Vacances/plage%20%C3%A9t%C3%A9.png&size=100", 400),
            ("root=images&path=Vacances/plage%20%C3%A9t%C3%A9.png&size=abc", 400),
            ("root=images&path=..%2F..%2Fetc%2Fpasswd&size=160", 400),
            ("root=images&path=notes.txt&size=160", 400),
            ("root=images&path=absent.png&size=160", 404),
            ("root=nope&path=a.png&size=160", 404),
        ):
            response = await client.get(f"/thumb?{query}&token={TOKEN}")
            self.assertEqual(response.status, status, query)


if __name__ == "__main__":
    unittest.main()

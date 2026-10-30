"""Tests des lanceurs : stockage SQLite, validation, construction de la commande, applications .desktop."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediadeck import launchers
from mediadeck.config import Launcher as ConfigLauncher
from mediadeck.launchers import (
    MAX_OPENERS_PER_TYPE,
    CommandNotAllowed,
    LauncherEntry,
    LauncherError,
    LauncherNotFound,
    LauncherStore,
    OpenerEntry,
    build_argv,
    build_open_argv,
    validate,
    validate_opener,
)

WEB = {"label": "Arte", "icon": "tv", "kind": "web", "spec": {"url": "https://www.arte.tv/fr/", "browser": "chromium"}}
COMMAND = {"label": "Off", "icon": "screen-off", "kind": "command", "spec": {"argv": ["kscreen-doctor", "--dpms", "off"]}}

DESKTOP = """[Desktop Entry]
Type=Application
Name=Lecteur {name}
Exec={name} %U
{extra}
"""


def write_desktop(directory: Path, filename: str, name: str, extra: str = "") -> None:
    (directory / filename).write_text(DESKTOP.format(name=name, extra=extra))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "launchers.db"
        self.store = LauncherStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_private_permissions(self) -> None:
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.path.parent.stat().st_mode & 0o077, 0)

    def test_create_generates_unique_slugs_and_keeps_order(self) -> None:
        first = self.store.create("Écran off", "app", "command", {"argv": ["a"]})
        second = self.store.create("Écran off", "app", "command", {"argv": ["b"]})
        self.assertEqual((first.id, second.id), ("ecran-off", "ecran-off-2"))
        self.assertEqual([entry.id for entry in self.store.list()], ["ecran-off", "ecran-off-2"])

    def test_update_and_delete(self) -> None:
        entry = self.store.create("Arte", "tv", "web", WEB["spec"])
        self.store.update(entry.id, "Arte TV", "film", "web", WEB["spec"])
        updated = self.store.get(entry.id)
        assert updated is not None
        self.assertEqual((updated.label, updated.icon), ("Arte TV", "film"))
        self.store.delete(entry.id)
        self.assertIsNone(self.store.get(entry.id))
        with self.assertRaises(LauncherNotFound):
            self.store.delete(entry.id)
        with self.assertRaises(LauncherNotFound):
            self.store.update("absent", "x", "app", "command", {"argv": ["x"]})

    def test_reorder_requires_every_id(self) -> None:
        ids = [self.store.create(name, "app", "command", {"argv": [name]}).id for name in ("a", "b", "c")]
        self.store.reorder(["c", "a", "b"])
        self.assertEqual([entry.id for entry in self.store.list()], ["c", "a", "b"])
        for bad in (ids[:2], [*ids, "d"], ["a", "a", "b"]):
            with self.subTest(ids=bad), self.assertRaises(LauncherError):
                self.store.reorder(bad)

    def test_data_survives_reopening(self) -> None:
        self.store.create("Arte", "tv", "web", WEB["spec"])
        self.store.close()
        self.store = LauncherStore(self.path)
        self.assertEqual([entry.label for entry in self.store.list()], ["Arte"])

    def test_seed_imports_config_launchers_once(self) -> None:
        config_launchers = [
            ConfigLauncher("vlc", "VLC", ("vlc",), "film"),
            ConfigLauncher("odd", "Odd", ("odd", "--x"), "inconnue"),
        ]
        self.assertEqual(self.store.seed(config_launchers), 2)
        self.assertEqual(self.store.seed(config_launchers), 0)
        entries = self.store.list()
        self.assertEqual([entry.id for entry in entries], ["vlc", "odd"])
        self.assertEqual(entries[0].kind, "command")
        self.assertEqual(entries[1].spec, {"argv": ["odd", "--x"]})
        self.assertEqual(entries[1].icon, "app")
        # Une fois importés, les lanceurs supprimés ne reviennent pas au redémarrage
        self.store.delete("vlc")
        self.store.close()
        self.store = LauncherStore(self.path)
        self.assertEqual(self.store.seed(config_launchers), 0)
        self.assertEqual([entry.id for entry in self.store.list()], ["odd"])

    def test_limit(self) -> None:
        for index in range(launchers.MAX_LAUNCHERS):
            self.store.create(f"l{index}", "app", "command", {"argv": ["x"]})
        with self.assertRaises(LauncherError):
            self.store.create("one more", "app", "command", {"argv": ["x"]})


class ValidateTest(unittest.TestCase):
    def test_valid_web(self) -> None:
        label, icon, kind, spec = validate(WEB, allow_command=False)
        self.assertEqual((label, icon, kind), ("Arte", "tv", "web"))
        self.assertEqual(spec, {"url": "https://www.arte.tv/fr/", "browser": "chromium", "fullscreen": False})

    def test_fullscreen_dropped_for_firefox(self) -> None:
        data = {**WEB, "spec": {**WEB["spec"], "browser": "firefox", "fullscreen": True}}
        self.assertFalse(validate(data, allow_command=False)[3]["fullscreen"])

    def test_rejected_urls(self) -> None:
        for url in ("javascript:alert(1)", "file:///etc/passwd", "http://", "https://a b.example", "--kiosk", "", " ",
                    "https://x/\n--evil", 5):
            with self.subTest(url=url), self.assertRaises(LauncherError):
                validate({**WEB, "spec": {**WEB["spec"], "url": url}}, allow_command=False)

    def test_rejected_fields(self) -> None:
        for patch in (
            {"label": ""}, {"label": "x" * 41}, {"label": 3}, {"icon": "nope"}, {"kind": "shell"}, {"kind": None},
            {"spec": "x"}, {"spec": {**WEB["spec"], "browser": "netscape"}}, {"spec": {**WEB["spec"], "fullscreen": "yes"}},
        ):
            with self.subTest(patch=patch), self.assertRaises(LauncherError):
                validate({**WEB, **patch}, allow_command=False)
        with self.assertRaises(LauncherError):
            validate([], allow_command=False)

    def test_command_forbidden_by_default(self) -> None:
        with self.assertRaises(CommandNotAllowed):
            validate(COMMAND, allow_command=False)
        self.assertEqual(validate(COMMAND, allow_command=True)[3], {"argv": ["kscreen-doctor", "--dpms", "off"]})

    def test_command_argv_rules(self) -> None:
        for argv in ([], "vlc", [""], ["a", 3], ["a\0b"], ["x"] * 33):
            with self.subTest(argv=argv), self.assertRaises(LauncherError):
                validate({**COMMAND, "spec": {"argv": argv}}, allow_command=True)

    def test_existing_command_can_be_renamed_but_not_changed(self) -> None:
        current = LauncherEntry("off", "Off", "app", "command", COMMAND["spec"])
        renamed = validate({**COMMAND, "label": "Écran éteint"}, allow_command=False, current=current)
        self.assertEqual(renamed[0], "Écran éteint")
        with self.assertRaises(CommandNotAllowed):
            validate({**COMMAND, "spec": {"argv": ["rm", "-rf", "~"]}}, allow_command=False, current=current)
        # Convertir un lanceur web en commande brute reste une création de commande
        web = LauncherEntry("w", "W", "tv", "web", WEB["spec"])
        with self.assertRaises(CommandNotAllowed):
            validate(COMMAND, allow_command=False, current=web)


class BuildArgvTest(unittest.TestCase):
    def test_web_argv(self) -> None:
        spec = {"url": "https://www.arte.tv/fr/", "browser": "chromium", "fullscreen": True}
        entry = LauncherEntry("arte", "Arte", "tv", "web", spec)
        with mock.patch("mediadeck.launchers.shutil.which", return_value="/usr/bin/chromium"):
            self.assertEqual(build_argv(entry), ["chromium", "--start-fullscreen", "https://www.arte.tv/fr/"])
            windowed = LauncherEntry("arte", "Arte", "tv", "web", {**spec, "fullscreen": False})
            self.assertEqual(build_argv(windowed), ["chromium", "https://www.arte.tv/fr/"])
            firefox = LauncherEntry("arte", "Arte", "tv", "web", {**spec, "browser": "firefox"})
            self.assertEqual(build_argv(firefox), ["firefox", "--new-window", "https://www.arte.tv/fr/"])

    def test_web_browser_not_installed(self) -> None:
        entry = LauncherEntry("arte", "Arte", "tv", "web", WEB["spec"])
        with mock.patch("mediadeck.launchers.shutil.which", return_value=None), self.assertRaises(LauncherError):
            build_argv(entry)

    def test_command_argv(self) -> None:
        entry = LauncherEntry("off", "Off", "app", "command", COMMAND["spec"])
        self.assertEqual(build_argv(entry), ["kscreen-doctor", "--dpms", "off"])

    def test_app_argv_uses_gio_and_the_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_desktop(Path(tmp), "vlc.desktop", "vlc")
            entry = LauncherEntry("vlc", "VLC", "film", "app", {"desktop_id": "vlc.desktop"})
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]), \
                    mock.patch("mediadeck.launchers.shutil.which", return_value="/usr/bin/gio"):
                self.assertEqual(build_argv(entry), ["gio", "launch", str(Path(tmp) / "vlc.desktop")])
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]), \
                    mock.patch("mediadeck.launchers.shutil.which", return_value=None), self.assertRaises(LauncherError):
                build_argv(entry)


class OpenerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "launchers.db"
        self.store = LauncherStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def ids(self, media_type: str) -> list[str]:
        return [entry.id for entry in self.store.openers()[media_type]]

    def test_system_default_until_one_is_chosen(self) -> None:
        openers = self.store.openers()
        self.assertEqual(list(openers), ["audio", "image", "video"])
        for media_type, entries in openers.items():
            (entry,) = entries
            self.assertEqual((entry.id, entry.label, entry.kind, entry.spec), (media_type, "", "default", {}))

    def test_several_players_per_type_in_order_and_surviving_reopening(self) -> None:
        mpv = self.store.create_opener("video", "mpv", "command", {"argv": ["mpv", "--fs"]})
        vlc = self.store.create_opener("video", "VLC", "app", {"desktop_id": "vlc.desktop"})
        # Le lecteur implicite devient le premier (par défaut) dès qu'on en ajoute un
        self.assertEqual(self.ids("video"), ["video", mpv.id, vlc.id])
        self.assertEqual(self.ids("audio"), ["audio"])  # les autres types ne changent pas
        self.store.close()
        self.store = LauncherStore(self.path)
        self.assertEqual(self.ids("video"), ["video", "mpv", "vlc"])
        self.assertEqual(self.store.get_opener("mpv"), mpv)

    def test_ids_come_from_the_label_and_never_collide_with_a_type(self) -> None:
        self.assertEqual(self.store.create_opener("audio", "Mon lecteur", "default", {}).id, "mon-lecteur")
        self.assertEqual(self.store.create_opener("video", "Mon lecteur", "default", {}).id, "mon-lecteur-2")
        self.assertEqual(self.store.create_opener("image", "Video", "default", {}).id, "video-2")  # « video » désigne un type
        self.assertEqual(self.store.create_opener("image", "!!!", "default", {}).id, "player")

    def test_update_of_the_implicit_player_registers_it(self) -> None:
        updated = self.store.update_opener("audio", "Rhythmbox", "app", {"desktop_id": "rb.desktop"})
        self.assertEqual((updated.id, updated.label, updated.kind), ("audio", "Rhythmbox", "app"))
        self.assertEqual(self.ids("audio"), ["audio"])
        self.assertEqual(self.store.openers()["audio"][0].spec, {"desktop_id": "rb.desktop"})
        with self.assertRaises(LauncherNotFound):
            self.store.update_opener("inconnu", "x", "default", {})

    def test_reorder_changes_the_default(self) -> None:
        self.store.create_opener("video", "mpv", "default", {})
        self.store.create_opener("video", "vlc", "default", {})
        self.store.reorder_openers("video", ["vlc", "video", "mpv"])
        self.assertEqual(self.ids("video"), ["vlc", "video", "mpv"])
        for bad in (["vlc", "video"], ["vlc", "video", "mpv", "mpv"], ["vlc", "video", "autre"], []):
            with self.subTest(ids=bad), self.assertRaises(LauncherError):
                self.store.reorder_openers("video", bad)
        with self.assertRaises(LauncherNotFound):
            self.store.reorder_openers("document", [])

    def test_a_type_always_keeps_a_player(self) -> None:
        self.store.create_opener("video", "mpv", "default", {})
        self.store.delete_opener("mpv")
        with self.assertRaises(LauncherError):
            self.store.delete_opener("video")  # le dernier
        self.store.create_opener("video", "vlc", "default", {})
        self.store.delete_opener("video")  # le premier part, l'ordre se resserre
        self.assertEqual(self.ids("video"), ["vlc"])
        with self.assertRaises(LauncherNotFound):
            self.store.delete_opener("inconnu")

    def test_limit_per_type(self) -> None:
        for number in range(MAX_OPENERS_PER_TYPE - 1):  # le lecteur implicite compte pour un
            self.store.create_opener("audio", f"lecteur {number}", "default", {})
        with self.assertRaises(LauncherError):
            self.store.create_opener("audio", "de trop", "default", {})

    def test_database_refuses_unknown_types(self) -> None:
        with self.assertRaises(Exception):
            self.store.create_opener("document", "x", "default", {})

    def test_opener_table_added_to_an_existing_database(self) -> None:
        # Base créée avant les lecteurs : seule la table des lanceurs existe
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE media_players")
        db.commit()
        db.close()
        self.store = LauncherStore(self.path)
        self.assertEqual(self.store.openers()["audio"][0].kind, "default")

    def test_single_players_of_the_previous_version_are_migrated(self) -> None:
        # Base d'avant les lecteurs multiples : une ligne par type dans media_openers
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE media_players")
        db.execute(
            "CREATE TABLE media_openers (type TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL)"
        )
        db.execute("INSERT INTO media_openers VALUES ('video', 'command', '{\"argv\": [\"mpv\", \"--fs\"]}')")
        db.execute("INSERT INTO media_openers VALUES ('audio', 'app', '{\"desktop_id\": \"rb.desktop\"}')")
        db.commit()
        db.close()
        self.store = LauncherStore(self.path)
        video = self.store.openers()["video"]
        self.assertEqual([(e.id, e.label, e.kind, e.spec) for e in video], [("video", "", "command", {"argv": ["mpv", "--fs"]})])
        self.assertEqual(self.store.openers()["audio"][0].spec, {"desktop_id": "rb.desktop"})
        self.assertEqual(self.store.openers()["image"][0].kind, "default")
        self.store.close()
        self.store = LauncherStore(self.path)  # la migration ne se rejoue pas
        self.assertEqual(self.ids("video"), ["video"])
        tables = {row[0] for row in sqlite3.connect(self.path).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("media_openers", tables)


class ValidateOpenerTest(unittest.TestCase):
    def test_default_needs_no_spec(self) -> None:
        self.assertEqual(
            validate_opener("audio", {"label": "Système", "kind": "default"}, allow_command=False),
            ("audio", "Système", "default", {}),
        )
        # Un spec fourni avec « default » est ignoré : rien d'autre n'est enregistré
        self.assertEqual(
            validate_opener("audio", {"label": "x", "kind": "default", "spec": {"argv": ["rm"]}}, allow_command=False),
            ("audio", "x", "default", {}),
        )

    def test_label_is_required_to_create_and_optional_to_update(self) -> None:
        for label in (None, "", "   ", 3, "x" * 41):
            with self.subTest(label=label), self.assertRaises(LauncherError):
                validate_opener("audio", {"label": label, "kind": "default"}, allow_command=False)
        with self.assertRaises(LauncherError):
            validate_opener("audio", {"kind": "default"}, allow_command=False)
        current = OpenerEntry("audio", "audio", "Mon lecteur", "default", {})
        self.assertEqual(validate_opener("audio", {"kind": "default"}, allow_command=False, current=current)[1], "Mon lecteur")
        self.assertEqual(
            validate_opener("audio", {"label": "  Autre ", "kind": "default"}, allow_command=False, current=current)[1], "Autre"
        )

    def test_app_must_be_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_desktop(Path(tmp), "vlc.desktop", "vlc")
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]):
                self.assertEqual(
                    validate_opener("video", {"label": "VLC", "kind": "app", "spec": {"desktop_id": "vlc.desktop"}}, allow_command=False),
                    ("video", "VLC", "app", {"desktop_id": "vlc.desktop"}),
                )
                with self.assertRaises(LauncherError):
                    validate_opener("video", {"label": "VLC", "kind": "app", "spec": {"desktop_id": "../vlc.desktop"}}, allow_command=False)

    def test_command_needs_the_config_switch(self) -> None:
        data = {"label": "mpv", "kind": "command", "spec": {"argv": ["mpv", "--fs"]}}
        with self.assertRaises(CommandNotAllowed):
            validate_opener("video", data, allow_command=False)
        self.assertEqual(
            validate_opener("video", data, allow_command=True), ("video", "mpv", "command", {"argv": ["mpv", "--fs"]})
        )
        # Une commande déjà enregistrée peut être « réenregistrée » telle quelle
        current = OpenerEntry("mpv", "video", "mpv", "command", {"argv": ["mpv", "--fs"]})
        self.assertEqual(validate_opener("video", data, allow_command=False, current=current)[2], "command")
        evil = {"label": "mpv", "kind": "command", "spec": {"argv": ["evil"]}}
        with self.assertRaises(CommandNotAllowed):
            validate_opener("video", evil, allow_command=False, current=current)

    def test_rejected_input(self) -> None:
        with self.assertRaises(LauncherNotFound):
            validate_opener("document", {"label": "x", "kind": "default"}, allow_command=True)
        for data in (
            None, [], {}, {"label": "x"}, {"label": "x", "kind": "web"}, {"label": "x", "kind": "command"},
            {"label": "x", "kind": "command", "spec": {"argv": []}},
        ):
            with self.subTest(data=data), self.assertRaises(LauncherError):
                validate_opener("audio", data, allow_command=True)


class BuildOpenArgvTest(unittest.TestCase):
    PATH = Path("/home/u/Musique/-weird name.mp3")

    def test_default_uses_gio_open(self) -> None:
        with mock.patch("mediadeck.launchers.shutil.which", return_value="/usr/bin/gio"):
            self.assertEqual(build_open_argv(OpenerEntry("audio", "audio", "", "default", {}), self.PATH), ["gio", "open", str(self.PATH)])
        with mock.patch("mediadeck.launchers.shutil.which", return_value=None), self.assertRaises(LauncherError):
            build_open_argv(OpenerEntry("audio", "audio", "", "default", {}), self.PATH)

    def test_app_uses_gio_launch_with_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_desktop(Path(tmp), "vlc.desktop", "vlc")
            opener = OpenerEntry("video", "video", "VLC", "app", {"desktop_id": "vlc.desktop"})
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]), \
                    mock.patch("mediadeck.launchers.shutil.which", return_value="/usr/bin/gio"):
                self.assertEqual(build_open_argv(opener, self.PATH), ["gio", "launch", str(Path(tmp) / "vlc.desktop"), str(self.PATH)])
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp) / "vide"]), self.assertRaises(LauncherError):
                build_open_argv(opener, self.PATH)

    def test_command_gets_the_path_as_last_argument_without_a_shell(self) -> None:
        opener = OpenerEntry("mpv", "video", "mpv", "command", {"argv": ["mpv", "--fs"]})
        self.assertEqual(build_open_argv(opener, self.PATH), ["mpv", "--fs", str(self.PATH)])
        tricky = Path("/home/u/V/$(reboot); rm -rf x.mkv")
        self.assertEqual(build_open_argv(opener, tricky)[-1], str(tricky))


class DesktopAppsTest(unittest.TestCase):
    def test_listing_filters_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            user, system = Path(first), Path(second)
            write_desktop(user, "mpv.desktop", "mpv-utilisateur")
            write_desktop(system, "mpv.desktop", "mpv-systeme")
            write_desktop(system, "zeal.desktop", "zeal")
            write_desktop(system, "hidden.desktop", "hidden", "NoDisplay=true")
            write_desktop(system, "shell.desktop", "shell", "Terminal=true")
            (system / "broken.desktop").write_text("pas un fichier desktop")
            (system / "notes.txt").write_text("[Desktop Entry]")
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[user, system, system / "absent"]):
                apps = launchers.list_desktop_apps()
                self.assertEqual([(app.id, app.name) for app in apps],
                                 [("mpv.desktop", "Lecteur mpv-utilisateur"), ("zeal.desktop", "Lecteur zeal")])
                self.assertIsNone(launchers.find_desktop_entry("hidden.desktop"))

    def test_find_desktop_entry_rejects_path_tricks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_desktop(Path(tmp), "vlc.desktop", "vlc")
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp) / "sub"]):
                for desktop_id in ("../vlc.desktop", "/etc/passwd", "vlc", "a/b.desktop", ""):
                    with self.subTest(desktop_id=desktop_id):
                        self.assertIsNone(launchers.find_desktop_entry(desktop_id))


if __name__ == "__main__":
    unittest.main()

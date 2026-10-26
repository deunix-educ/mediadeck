"""Tests de l'API REST d'administration des lanceurs (sans D-Bus : seules les routes /api sont montées)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mediadeck.admin import AdminApi
from mediadeck.config import parse_config
from mediadeck.launchers import LauncherStore

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WEB = {"label": "Arte", "icon": "tv", "kind": "web", "spec": {"url": "https://www.arte.tv/fr/", "browser": "chromium"}}
COMMAND = {"label": "Off", "icon": "screen-off", "kind": "command", "spec": {"argv": ["kscreen-doctor", "--dpms", "off"]}}


class AdminApiCase(unittest.IsolatedAsyncioTestCase):
    """Serveur de test sans commande brute autorisée ; les sous-classes ajoutent les tests."""

    allow_command = False

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LauncherStore(Path(self._tmp.name) / "launchers.db")
        self.changes = 0
        config = parse_config(
            {"server": {"token": TOKEN}, "admin": {"allow_custom_command": self.allow_command}}
        )
        app = web.Application()
        AdminApi(config, self.store, self._on_change).add_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    def _on_change(self) -> None:
        self.changes += 1


class AdminApiTest(AdminApiCase):
    async def test_token_required_on_every_route(self) -> None:
        calls = [
            ("GET", "/api/launchers"), ("POST", "/api/launchers"), ("PUT", "/api/launchers/x"),
            ("DELETE", "/api/launchers/x"), ("POST", "/api/launchers/reorder"), ("GET", "/api/apps"),
        ]
        wrong = {"Authorization": "Bearer nope"}
        for method, path in calls:
            for headers in ({}, wrong, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}):
                with self.subTest(method=method, path=path, headers=headers):
                    response = await self.client.request(method, path, headers=headers, json=WEB)
                    self.assertEqual(response.status, 401)
        # Le jeton en paramètre d'URL n'est pas accepté : il finirait dans les journaux et l'historique
        response = await self.client.get(f"/api/launchers?token={TOKEN}")
        self.assertEqual(response.status, 401)

    async def test_list_exposes_options(self) -> None:
        response = await self.client.get("/api/launchers", headers=AUTH)
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["launchers"], [])
        self.assertIn("tv", data["icons"])
        self.assertFalse(data["allow_command"])
        self.assertEqual({browser["id"] for browser in data["browsers"]}, {"chromium", "chrome", "brave", "firefox"})

    async def test_crud_cycle(self) -> None:
        created = await self.client.post("/api/launchers", headers=AUTH, json=WEB)
        self.assertEqual(created.status, 201)
        entry = await created.json()
        self.assertEqual(entry["id"], "arte")
        self.assertEqual(entry["spec"]["fullscreen"], False)

        updated = await self.client.put("/api/launchers/arte", headers=AUTH, json={**WEB, "label": "Arte TV"})
        self.assertEqual(updated.status, 200)
        self.assertEqual((await updated.json())["label"], "Arte TV")

        second = await self.client.post("/api/launchers", headers=AUTH, json={**WEB, "label": "Autre"})
        self.assertEqual(second.status, 201)
        reorder = await self.client.post("/api/launchers/reorder", headers=AUTH, json={"ids": ["autre", "arte"]})
        self.assertEqual(reorder.status, 204)
        listing = await (await self.client.get("/api/launchers", headers=AUTH)).json()
        self.assertEqual([item["id"] for item in listing["launchers"]], ["autre", "arte"])

        deleted = await self.client.delete("/api/launchers/arte", headers=AUTH)
        self.assertEqual(deleted.status, 204)
        self.assertEqual((await self.client.delete("/api/launchers/arte", headers=AUTH)).status, 404)
        self.assertEqual((await self.client.put("/api/launchers/arte", headers=AUTH, json=WEB)).status, 404)
        # create + update + create + reorder + delete : chaque modification notifie les tablettes
        self.assertEqual(self.changes, 5)

    async def test_validation_errors_do_not_change_anything(self) -> None:
        bad = {**WEB, "spec": {**WEB["spec"], "url": "javascript:alert(1)"}}
        response = await self.client.post("/api/launchers", headers=AUTH, json=bad)
        self.assertEqual(response.status, 400)
        self.assertIn("error", await response.json())
        invalid_json = await self.client.post("/api/launchers", headers=AUTH, data=b"{nope")
        self.assertEqual(invalid_json.status, 400)
        too_large = await self.client.post("/api/launchers", headers=AUTH, data=b" " * 20000)
        self.assertEqual(too_large.status, 400)
        bad_order = await self.client.post("/api/launchers/reorder", headers=AUTH, json={"ids": ["x"]})
        self.assertEqual(bad_order.status, 400)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.changes, 0)

    async def test_raw_commands_are_forbidden_by_default(self) -> None:
        response = await self.client.post("/api/launchers", headers=AUTH, json=COMMAND)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.store.list(), [])

    async def test_imported_command_can_be_renamed_but_not_rewritten(self) -> None:
        entry = self.store.create("Off", "screen-off", "command", COMMAND["spec"])
        renamed = await self.client.put(f"/api/launchers/{entry.id}", headers=AUTH, json={**COMMAND, "label": "Écran off"})
        self.assertEqual(renamed.status, 200)
        rewritten = await self.client.put(
            f"/api/launchers/{entry.id}", headers=AUTH, json={**COMMAND, "spec": {"argv": ["reboot"]}}
        )
        self.assertEqual(rewritten.status, 403)
        stored = self.store.get(entry.id)
        assert stored is not None
        self.assertEqual(stored.spec, COMMAND["spec"])

    async def test_apps_listing_and_app_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "vlc.desktop").write_text("[Desktop Entry]\nType=Application\nName=VLC\nExec=vlc\n")
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]):
                apps = await (await self.client.get("/api/apps", headers=AUTH)).json()
                self.assertEqual(apps, [{"id": "vlc.desktop", "name": "VLC"}])
                ok = await self.client.post(
                    "/api/launchers", headers=AUTH,
                    json={"label": "VLC", "icon": "film", "kind": "app", "spec": {"desktop_id": "vlc.desktop"}},
                )
                self.assertEqual(ok.status, 201)
                unknown = await self.client.post(
                    "/api/launchers", headers=AUTH,
                    json={"label": "X", "icon": "app", "kind": "app", "spec": {"desktop_id": "../../x.desktop"}},
                )
                self.assertEqual(unknown.status, 400)


class OpenersApiTest(AdminApiCase):
    async def test_token_required(self) -> None:
        for method, path in (("GET", "/api/media/openers"), ("PUT", "/api/media/openers/audio")):
            for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}):
                response = await self.client.request(method, path, headers=headers, json={"kind": "default"})
                self.assertEqual(response.status, 401, (method, headers))

    async def test_list_defaults_to_the_system_application(self) -> None:
        data = await (await self.client.get("/api/media/openers", headers=AUTH)).json()
        self.assertEqual([opener["type"] for opener in data["openers"]], ["audio", "image", "video"])
        self.assertTrue(all(opener["kind"] == "default" for opener in data["openers"]))
        self.assertEqual(data["kinds"], ["default", "app", "command"])
        self.assertFalse(data["allow_command"])

    async def test_set_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "vlc.desktop").write_text("[Desktop Entry]\nType=Application\nName=VLC\nExec=vlc\n")
            with mock.patch("mediadeck.launchers.desktop_dirs", return_value=[Path(tmp)]):
                response = await self.client.put(
                    "/api/media/openers/video", headers=AUTH, json={"kind": "app", "spec": {"desktop_id": "vlc.desktop"}}
                )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"type": "video", "kind": "app", "spec": {"desktop_id": "vlc.desktop"}})
        data = await (await self.client.get("/api/media/openers", headers=AUTH)).json()
        self.assertEqual({o["type"]: o["kind"] for o in data["openers"]}, {"audio": "default", "image": "default", "video": "app"})
        back = await self.client.put("/api/media/openers/video", headers=AUTH, json={"kind": "default"})
        self.assertEqual((await back.json())["kind"], "default")

    async def test_errors(self) -> None:
        unknown_type = await self.client.put("/api/media/openers/document", headers=AUTH, json={"kind": "default"})
        self.assertEqual(unknown_type.status, 404)
        for body in ({"kind": "web"}, {"kind": "app", "spec": {"desktop_id": "nope.desktop"}}, {}):
            response = await self.client.put("/api/media/openers/audio", headers=AUTH, json=body)
            self.assertEqual(response.status, 400, body)
        forbidden = await self.client.put(
            "/api/media/openers/audio", headers=AUTH, json={"kind": "command", "spec": {"argv": ["mpv"]}}
        )
        self.assertEqual(forbidden.status, 403)
        self.assertTrue(all(opener.kind == "default" for opener in self.store.openers().values()))


class OpenersApiCommandAllowedTest(AdminApiCase):
    allow_command = True

    async def test_command_opener(self) -> None:
        response = await self.client.put(
            "/api/media/openers/video", headers=AUTH, json={"kind": "command", "spec": {"argv": ["mpv", "--fs"]}}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.openers()["video"].spec, {"argv": ["mpv", "--fs"]})


class AdminApiCommandAllowedTest(AdminApiCase):
    allow_command = True

    async def test_list_announces_raw_commands(self) -> None:
        data = await (await self.client.get("/api/launchers", headers=AUTH)).json()
        self.assertTrue(data["allow_command"])

    async def test_raw_commands_are_accepted(self) -> None:
        response = await self.client.post("/api/launchers", headers=AUTH, json=COMMAND)
        self.assertEqual(response.status, 201)
        self.assertEqual((await response.json())["spec"], COMMAND["spec"])

    async def test_command_can_be_rewritten(self) -> None:
        entry = self.store.create("Off", "screen-off", "command", COMMAND["spec"])
        rewritten = await self.client.put(
            f"/api/launchers/{entry.id}", headers=AUTH, json={**COMMAND, "spec": {"argv": ["reboot"]}}
        )
        self.assertEqual(rewritten.status, 200)


if __name__ == "__main__":
    unittest.main()

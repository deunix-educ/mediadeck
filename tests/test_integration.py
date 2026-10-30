"""Scénario complet : vrai serveur (HTTP + WebSocket), bus D-Bus de session privé, faux lecteur MPRIS, faux gio et pactl.

L'environnement isolé vient de ``stack.py`` (rien ne touche au bureau de l'utilisateur). Ignoré sans dbus-daemon.
"""

import asyncio
import json
import unittest
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient
from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method
from dbus_next.signature import Variant

from mediadeck.mpris import MPRIS_PATH, MPRIS_PREFIX
from stack import DBUS_DAEMON, PNG, TOKEN, TRICKY_NAME, Stack

PLAYER_NAME = MPRIS_PREFIX + "fakeplayer"


class FakeRoot(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2")

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":  # type: ignore[name-defined]  # noqa: F821
        return "Fake player"

    @method()
    def Raise(self) -> None:
        pass


class FakePlayer(ServiceInterface):
    """Lecteur sans titre : il annonce seulement le fichier (xesam:url) et sa pochette locale, comme Celluloid."""

    def __init__(self, url: str, art: str) -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.url = url
        self.art = art
        self.volume = 1.0
        self.calls: list[tuple[str, Any]] = []

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":  # type: ignore[name-defined]  # noqa: F821
        return "Playing"

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":  # type: ignore[name-defined]  # noqa: F821
        return {"xesam:url": Variant("s", self.url), "mpris:artUrl": Variant("s", self.art)}

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":  # type: ignore[name-defined]  # noqa: F821
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":  # type: ignore[name-defined]  # noqa: F821
        return True

    @dbus_property()
    def Volume(self) -> "d":  # type: ignore[name-defined]  # noqa: F821
        return self.volume

    @Volume.setter
    def Volume(self, value: "d") -> None:  # type: ignore[name-defined]  # noqa: F821
        self.volume = value
        self.calls.append(("volume", value))

    @method()
    def PlayPause(self) -> None:
        self.calls.append(("play_pause", None))

    @method()
    def Seek(self, offset: "x") -> None:  # type: ignore[name-defined]  # noqa: F821
        self.calls.append(("seek", offset))


@unittest.skipUnless(DBUS_DAEMON, "dbus-daemon not installed")
class FullScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        warnings.filterwarnings("ignore", message="unclosed <socket", category=ResourceWarning)
        self.stack = Stack()
        await self.stack.start()
        self.addAsyncCleanup(self.stack.close)
        self.client: TestClient = self.stack.client
        self.root = self.stack.root
        self.tricky = self.stack.tricky
        self.art_file = self.stack.art_file

        self.player_bus = await MessageBus(bus_address=self.stack.address).connect()
        self.addCleanup(self.player_bus.disconnect)
        self.player = FakePlayer((self.root / "Albums" / "morceau.mp3").as_uri(), self.art_file.as_uri())
        self.player_bus.export(MPRIS_PATH, FakeRoot())
        self.player_bus.export(MPRIS_PATH, self.player)
        await self.player_bus.request_name(PLAYER_NAME)

        self.ws = await self.client.ws_connect(f"/ws?token={TOKEN}")
        self.addAsyncCleanup(self.ws.close)

    # ------------------------------------------------------------------
    # Outils
    # ------------------------------------------------------------------

    async def until(self, condition: Callable[[], Any], seconds: float = 5.0) -> bool:
        for _ in range(int(seconds / 0.05)):
            result = condition()
            if (await result) if isinstance(result, Awaitable) else result:
                return True
            await asyncio.sleep(0.05)
        return False

    def tool_calls(self, tool: str) -> list[list[str]]:
        return self.stack.tool_calls(tool)

    async def message(self, accept: Callable[[dict[str, Any]], bool], seconds: float = 5.0) -> dict[str, Any]:
        """Prochain message du serveur qui satisfait ``accept`` (les autres sont ignorés)."""
        async with asyncio.timeout(seconds):
            while True:
                data = await self.ws.receive_json()
                if accept(data):
                    return data

    async def state_with(self, accept: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return await self.message(lambda data: data.get("type") == "state" and accept(data))

    async def send(self, payload: dict[str, Any], seconds: float) -> dict[str, Any] | None:
        """Envoie une commande ; renvoie l'erreur reçue pendant ``seconds`` au plus (None si le serveur se tait)."""
        await self.ws.send_json(payload)
        try:
            async with asyncio.timeout(seconds):
                while True:
                    data = await self.ws.receive_json()
                    if data.get("type") == "error":
                        return data
        except TimeoutError:
            return None

    async def accepted(self, **payload: Any) -> None:
        """La commande ne produit aucune erreur (le serveur ne confirme pas : on laisse un court délai à l'erreur)."""
        error = await self.send(payload, 0.2)
        self.assertIsNone(error, payload)

    async def refused(self, **payload: Any) -> dict[str, Any]:
        error = await self.send(payload, 3.0)
        assert error is not None, f"command not refused: {payload}"
        return error

    def media_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    async def root_id(self) -> str:
        state = await self.state_with(lambda data: bool(data["system"]["media"]["roots"]))
        return state["system"]["media"]["roots"][0]["id"]

    # ------------------------------------------------------------------
    # Authentification
    # ------------------------------------------------------------------

    async def test_every_entry_point_refuses_a_missing_or_misplaced_token(self) -> None:
        for path in ("/ws", "/ws?token=wrong", "/art?player=x", "/api/launchers", "/api/media", "/api/media/search?q=ab"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 401, path)
        # Le jeton d'une API REST se passe en en-tête, jamais dans l'URL
        response = await self.client.get(f"/api/launchers?token={TOKEN}")
        self.assertEqual(response.status, 401)
        response = await self.client.get("/api/launchers", headers=self.media_headers())
        self.assertEqual(response.status, 200)

    # ------------------------------------------------------------------
    # Lecteur MPRIS
    # ------------------------------------------------------------------

    async def test_player_is_listed_enriched_and_controlled(self) -> None:
        state = await self.state_with(lambda data: bool(data["mpris"]["players"]))
        player = state["mpris"]["players"][0]
        self.assertEqual(player["name"], PLAYER_NAME)
        self.assertEqual(player["metadata"]["title"], "morceau")  # complété depuis le nom du fichier de la bibliothèque
        art = player["metadata"]["art"]
        self.assertTrue(art.startswith("/art?player="), art)  # jamais le chemin local file://
        self.assertNotIn(self.art_file.as_uri(), json.dumps(state))
        self.assertNotIn(str(self.art_file), json.dumps(state))

        self.assertEqual((await self.client.get(art)).status, 401)
        response = await self.client.get(f"{art}&token={TOKEN}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(await response.read(), PNG)

        await self.accepted(domain="player", action="play_pause", player=PLAYER_NAME)
        await self.accepted(domain="player", action="seek", player=PLAYER_NAME, value=5_000_000)
        await self.accepted(domain="player", action="set_volume", player=PLAYER_NAME, value=0.5)
        self.assertEqual(self.player.calls, [("play_pause", None), ("seek", 5_000_000), ("volume", 0.5)])

    async def test_invalid_player_commands_are_reported(self) -> None:
        await self.state_with(lambda data: bool(data["mpris"]["players"]))
        for payload in (
            {"domain": "player", "action": "play_pause", "player": "org.mpris.MediaPlayer2.nobody"},
            {"domain": "player", "action": "play_pause"},
            {"domain": "player", "action": "seek", "player": PLAYER_NAME, "value": "far"},
            {"domain": "player", "action": "explode", "player": PLAYER_NAME},
            {"domain": "nothing", "action": "x"},
        ):
            await self.refused(**payload)
        self.assertEqual(self.player.calls, [])

    async def test_player_leaving_the_bus_disappears_from_the_state(self) -> None:
        await self.state_with(lambda data: bool(data["mpris"]["players"]))
        self.player_bus.disconnect()
        state = await self.state_with(lambda data: not data["mpris"]["players"])
        self.assertTrue(state["mpris"]["available"])

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------

    async def test_audio_commands_reach_pactl_for_known_sinks_only(self) -> None:
        state = await self.state_with(lambda data: bool(data["audio"]["sinks"]))
        self.assertEqual(state["audio"]["sinks"][0]["name"], "fake")
        self.assertEqual(state["audio"]["sinks"][0]["volume"], 0.5)
        await self.accepted(domain="audio", action="set_volume", sink=None, value=0.4)
        await self.accepted(domain="audio", action="toggle_mute", sink="fake")
        await self.refused(domain="audio", action="set_volume", sink="--evil", value=1)
        calls = self.tool_calls("pactl")
        self.assertIn(["set-sink-volume", "fake", "40%"], calls)
        self.assertIn(["set-sink-mute", "fake", "toggle"], calls)
        self.assertFalse([call for call in calls if "--evil" in call])

    # ------------------------------------------------------------------
    # Bibliothèque de médias
    # ------------------------------------------------------------------

    async def test_media_listing_and_escapes(self) -> None:
        root = await self.root_id()
        headers = self.media_headers()
        response = await self.client.get("/api/media", params={"root": root, "path": ""}, headers=headers)
        self.assertEqual(response.status, 200)
        names = {entry["name"] for entry in (await response.json())["entries"]}
        self.assertIn(TRICKY_NAME, names)
        self.assertIn("Albums", names)
        self.assertNotIn(".secret.mp3", names)  # caché
        self.assertNotIn("notes.sh", names)  # extension hors liste

        for path in ("..", "../..", "/etc", "Albums/../..", ".secret.mp3", ".hidden/x", "a//b", TRICKY_NAME):
            response = await self.client.get("/api/media", params={"root": root, "path": path}, headers=headers)
            self.assertEqual(response.status, 400, path)  # TRICKY_NAME est un fichier, pas un dossier
        response = await self.client.get("/api/media", params={"root": "inconnu", "path": ""}, headers=headers)
        self.assertEqual(response.status, 404)

    # ------------------------------------------------------------------
    # Ouverture de fichiers et lanceurs
    # ------------------------------------------------------------------

    async def test_open_media_runs_the_opener_without_a_shell_and_refuses_bad_paths(self) -> None:
        root = await self.root_id()
        for path in ("..", "/etc/passwd", ".secret.mp3", "notes.sh", "absent.mp3", "Albums", "script.mp3", ""):
            await self.refused(domain="system", action="open_media", root=root, path=path)
        await self.refused(domain="system", action="open_media", root="inconnu", path="a.mp3")
        self.assertEqual(self.tool_calls("gio"), [])  # aucun refus n'a lancé quoi que ce soit

        await self.accepted(domain="system", action="open_media", root=root, path=TRICKY_NAME)
        self.assertTrue(await self.until(lambda: self.tool_calls("gio")))
        self.assertEqual(self.tool_calls("gio"), [["open", str(self.tricky)]])
        state = await self.state_with(lambda data: data["system"]["media"]["last"] is not None)
        self.assertEqual(state["system"]["media"]["last"]["name"], Path(TRICKY_NAME).stem)

    async def test_open_media_with_a_chosen_player_among_several(self) -> None:
        root = await self.root_id()
        headers = self.media_headers()
        # Deux lecteurs de plus pour le son (une application et le défaut du système), un pour les images
        app = {"type": "audio", "label": "Appli", "kind": "app", "spec": {"desktop_id": "fake.desktop"}}
        response = await self.client.post("/api/media/openers", json=app, headers=headers)
        self.assertEqual(response.status, 201, await response.text())
        response = await self.client.post(
            "/api/media/openers", json={"type": "image", "label": "Visionneuse", "kind": "default"}, headers=headers
        )
        self.assertEqual(response.status, 201)

        # La tablette reçoit les lecteurs de chaque type (nom et genre), jamais la commande ni l'application
        state = await self.state_with(lambda data: len(data["system"]["media"]["openers"]["audio"]) == 2)
        players = state["system"]["media"]["openers"]
        self.assertEqual(players["audio"], [{"id": "audio", "label": "", "kind": "default"}, {"id": "appli", "label": "Appli", "kind": "app"}])
        self.assertEqual(len(players["image"]), 2)
        self.assertNotIn("fake.desktop", json.dumps(state["system"]["media"]))

        # Sans choix : le premier lecteur du type ; avec choix : celui-là
        await self.accepted(domain="system", action="open_media", root=root, path=TRICKY_NAME)
        self.assertTrue(await self.until(lambda: len(self.tool_calls("gio")) == 1))
        await self.accepted(domain="system", action="open_media", root=root, path=TRICKY_NAME, opener="appli")
        self.assertTrue(await self.until(lambda: len(self.tool_calls("gio")) == 2))
        self.assertEqual(
            self.tool_calls("gio"),
            [["open", str(self.tricky)], ["launch", str(self.stack.desktop), str(self.tricky)]],
        )

        # Un lecteur inconnu, d'un autre type ou mal formé est refusé sans rien lancer
        for opener in ("inconnu", "visionneuse", "", 3, ["appli"], {"id": "appli"}):
            await self.refused(domain="system", action="open_media", root=root, path=TRICKY_NAME, opener=opener)
        self.assertEqual(len(self.tool_calls("gio")), 2)

        # Supprimer le lecteur choisi : l'identifiant ne désigne plus rien
        self.assertEqual((await self.client.delete("/api/media/openers/appli", headers=headers)).status, 204)
        await self.state_with(lambda data: len(data["system"]["media"]["openers"]["audio"]) == 1)
        await self.refused(domain="system", action="open_media", root=root, path=TRICKY_NAME, opener="appli")

    async def test_configured_launcher_runs_its_argv_verbatim(self) -> None:
        state = await self.state_with(lambda data: bool(data["system"]["launchers"]))
        self.assertEqual([launcher["id"] for launcher in state["system"]["launchers"]], ["rec"])
        await self.accepted(domain="system", action="launch", id="rec")
        self.assertTrue(await self.until(lambda: self.tool_calls("recorder")))
        self.assertEqual(self.tool_calls("recorder"), [["--fs", "$(reboot)"]])  # pas de shell
        for launcher_id in ("inconnu", "", 3, None):
            await self.refused(domain="system", action="launch", id=launcher_id)
        self.assertEqual(len(self.tool_calls("recorder")), 1)

    async def test_admin_changes_reach_connected_clients_and_commands_stay_locked(self) -> None:
        headers = self.media_headers()
        site: dict[str, Any] = {"label": "Site", "icon": "globe", "kind": "web"}
        site["spec"] = {"url": "https://example.org/", "browser": "firefox", "fullscreen": False}
        response = await self.client.post("/api/launchers", json=site, headers=headers)
        self.assertEqual(response.status, 201, await response.text())
        created = (await response.json())["id"]
        state = await self.state_with(lambda data: created in [item["id"] for item in data["system"]["launchers"]])
        self.assertEqual(len(state["system"]["launchers"]), 2)  # la tablette reçoit la liste sans recharger

        # Une commande brute reste refusée tant que allow_custom_command est faux (lanceur comme lecteur)
        command = {"label": "Shell", "icon": "app", "kind": "command", "spec": {"argv": ["recorder"]}}
        response = await self.client.post("/api/launchers", json=command, headers=headers)
        self.assertEqual(response.status, 403)
        response = await self.client.put(
            "/api/media/openers/audio", json={"kind": "command", "spec": {"argv": ["recorder"]}}, headers=headers
        )
        self.assertEqual(response.status, 403)

        # Un lanceur supprimé n'est plus lançable
        response = await self.client.delete("/api/launchers/rec", headers=headers)
        self.assertEqual(response.status, 204)
        await self.state_with(lambda data: "rec" not in [item["id"] for item in data["system"]["launchers"]])
        await self.refused(domain="system", action="launch", id="rec")
        self.assertEqual(self.tool_calls("recorder"), [])


if __name__ == "__main__":
    unittest.main()

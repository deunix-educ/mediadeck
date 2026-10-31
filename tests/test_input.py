"""Boutons de souris maintenus (bascule) : état, relâchement garanti, interaction avec les clics du pavé."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from evdev import ecodes

from mediadeck import server as server_module
from mediadeck.config import parse_config
from mediadeck.input import VirtualInput
from mediadeck.launchers import LauncherStore

BTN_LEFT, BTN_RIGHT = ecodes.BTN_LEFT, ecodes.BTN_RIGHT
TOKEN = "i" * 32


class Device:
    def __init__(self) -> None:
        self.written: list[tuple[int, int, int]] = []
        self.synced = 0

    def write(self, event_type: int, code: int, value: int) -> None:
        self.written.append((event_type, code, value))

    def syn(self) -> None:
        self.synced += 1

    def close(self) -> None:
        pass

    def buttons(self) -> list[tuple[int, int]]:
        """Événements de boutons (code, valeur) dans l'ordre."""
        return [(code, value) for event_type, code, value in self.written if event_type == ecodes.EV_KEY]


def make_input() -> tuple[VirtualInput, Device]:
    virtual = VirtualInput(False)
    virtual._keyboard = Device()  # noqa: SLF001
    pointer = Device()
    virtual._pointer = pointer  # noqa: SLF001
    return virtual, pointer


class HeldButtonsTest(unittest.TestCase):
    def test_press_and_release_are_written_and_reported(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1)])
        self.assertEqual(virtual.snapshot()["buttons"], ["left"])
        virtual.execute("button", {"button": "left", "pressed": False})
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1), (BTN_LEFT, 0)])
        self.assertEqual(virtual.snapshot()["buttons"], [])
        self.assertEqual(pointer.synced, 2)

    def test_both_buttons_can_be_held_together(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("button", {"button": "right", "pressed": True})
        self.assertEqual(virtual.snapshot()["buttons"], ["left", "right"])
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1), (BTN_RIGHT, 1)])

    def test_repeating_a_state_writes_nothing(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": False})  # déjà relâché
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("button", {"button": "left", "pressed": True})  # déjà enfoncé
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1)])

    def test_a_click_on_a_held_button_does_not_release_it(self) -> None:
        # Un tapotement du pavé pendant un glissement ne doit pas lâcher le bouton
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("click", {"button": "left"})
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1)])
        self.assertEqual(virtual.snapshot()["buttons"], ["left"])

    def test_a_click_on_another_button_still_works(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("click", {"button": "right"})
        self.assertEqual(pointer.buttons(), [(BTN_LEFT, 1), (BTN_RIGHT, 1), (BTN_RIGHT, 0)])

    def test_moving_while_held_is_a_drag(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("move", {"dx": 5, "dy": -3})
        self.assertEqual([event for event in pointer.written if event[0] == ecodes.EV_REL],
                         [(ecodes.EV_REL, ecodes.REL_X, 5), (ecodes.EV_REL, ecodes.REL_Y, -3)])

    def test_invalid_requests_write_nothing(self) -> None:
        virtual, pointer = make_input()
        for payload in (
            {"button": "left"},  # pressed absent
            {"button": "left", "pressed": "true"},
            {"button": "left", "pressed": 1},
            {"button": "side", "pressed": True},
            {"button": None, "pressed": True},
            {"pressed": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                virtual.execute("button", payload)
        self.assertEqual(pointer.written, [])
        self.assertEqual(virtual.snapshot()["buttons"], [])

    def test_release_owned_by_only_frees_the_buttons_of_that_owner(self) -> None:
        virtual, pointer = make_input()
        first, second = object(), object()
        virtual.execute("button", {"button": "left", "pressed": True}, owner=first)
        virtual.execute("button", {"button": "right", "pressed": True}, owner=second)
        self.assertTrue(virtual.release_owned_by(first))
        self.assertEqual(virtual.snapshot()["buttons"], ["right"])
        self.assertEqual(pointer.buttons()[-1], (BTN_LEFT, 0))
        self.assertFalse(virtual.release_owned_by(first))  # rien d'autre à relâcher

    def test_closing_releases_everything_first(self) -> None:
        virtual, pointer = make_input()
        virtual.execute("button", {"button": "left", "pressed": True})
        virtual.execute("button", {"button": "right", "pressed": True})
        virtual.close()
        self.assertEqual(sorted(pointer.buttons()[2:]), sorted([(BTN_LEFT, 0), (BTN_RIGHT, 0)]))
        self.assertEqual(virtual.snapshot()["buttons"], [])

    def test_unavailable_input_refuses_buttons(self) -> None:
        with self.assertRaises(RuntimeError):
            VirtualInput(False).execute("button", {"button": "left", "pressed": True})


class HeldButtonsOverWebSocketTest(unittest.IsolatedAsyncioTestCase):
    """Le serveur complet, sans bus D-Bus ni audio : un client qui disparaît relâche ses boutons."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        config = parse_config({"server": {"token": TOKEN, "host": "127.0.0.1"}, "media": {"roots": []}})
        patcher = mock.patch.object(server_module, "LauncherStore", lambda: LauncherStore(base / "launchers.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.deck = server_module.MediaDeckServer(config)
        self.pointer = Device()
        self.deck.input._keyboard = Device()  # noqa: SLF001
        self.deck.input._pointer = self.pointer  # noqa: SLF001
        app = self.deck.build_app()
        app.on_startup.clear()  # ni bus D-Bus, ni pactl
        app.on_shutdown.clear()
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()
        self.deck.launchers.close()
        self._tmp.cleanup()

    async def connect(self) -> Any:
        return await self.session.ws_connect(f"http://127.0.0.1:{self.server.port}/ws?token={TOKEN}")

    async def until(self, condition: Any, seconds: float = 3.0) -> bool:
        for _ in range(int(seconds / 0.05)):
            if condition():
                return True
            await asyncio.sleep(0.05)
        return condition()

    async def test_state_reports_held_buttons_to_every_client(self) -> None:
        first, second = await self.connect(), await self.connect()
        await first.receive_json()
        await second.receive_json()
        await first.send_json({"domain": "input", "action": "button", "button": "left", "pressed": True})
        seen = None
        for _ in range(10):
            message = await asyncio.wait_for(second.receive_json(), 3)
            if message.get("type") == "state" and message["input"]["buttons"] == ["left"]:
                seen = message
                break
        self.assertIsNotNone(seen)
        await first.close()
        await second.close()

    async def test_a_dropped_client_releases_its_buttons(self) -> None:
        client = await self.connect()
        await client.receive_json()
        await client.send_json({"domain": "input", "action": "button", "button": "left", "pressed": True})
        self.assertTrue(await self.until(lambda: self.pointer.buttons() == [(BTN_LEFT, 1)]))
        await client.close()
        self.assertTrue(await self.until(lambda: self.pointer.buttons() == [(BTN_LEFT, 1), (BTN_LEFT, 0)]))
        self.assertEqual(self.deck.input.snapshot()["buttons"], [])

    async def test_another_client_keeps_its_buttons_when_one_leaves(self) -> None:
        holder, other = await self.connect(), await self.connect()
        await holder.receive_json()
        await other.receive_json()
        await holder.send_json({"domain": "input", "action": "button", "button": "right", "pressed": True})
        self.assertTrue(await self.until(lambda: self.pointer.buttons() == [(BTN_RIGHT, 1)]))
        await other.close()
        await asyncio.sleep(0.3)
        self.assertEqual(self.pointer.buttons(), [(BTN_RIGHT, 1)])
        await holder.close()
        self.assertTrue(await self.until(lambda: self.pointer.buttons() == [(BTN_RIGHT, 1), (BTN_RIGHT, 0)]))


if __name__ == "__main__":
    unittest.main()

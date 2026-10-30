"""Tests de la découverte MPRIS sur un bus de session privé (dbus-daemon). Ignorés sans dbus-daemon."""

import asyncio
import logging
import shutil
import subprocess
import unittest
import warnings
from unittest import mock
from collections.abc import Awaitable, Callable
from typing import Any

from dbus_next.message import Message
from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method
from dbus_next.signature import Variant

from mediadeck import mpris
from mediadeck.mpris import MPRIS_PATH, MPRIS_PREFIX, MprisManager

DBUS_DAEMON = shutil.which("dbus-daemon")
NAME = MPRIS_PREFIX + "fixedname"  # nom fixe, comme Dragon Player, Celluloid ou VLC


class FakeRoot(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2")

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":  # type: ignore[name-defined]  # noqa: F821
        return "Fake"

    @method()
    def Raise(self) -> None:
        pass


class FakePlayer(ServiceInterface):
    def __init__(self, title: str) -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.title = title

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":  # type: ignore[name-defined]  # noqa: F821
        return "Playing"

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":  # type: ignore[name-defined]  # noqa: F821
        return {"xesam:title": Variant("s", self.title)}

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":  # type: ignore[name-defined]  # noqa: F821
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":  # type: ignore[name-defined]  # noqa: F821
        return True

    @method()
    def PlayPause(self) -> None:
        pass


def empty_introspection(message: Message) -> Message | None:
    if message.interface == "org.freedesktop.DBus.Introspectable" and message.member == "Introspect":
        return Message.new_method_return(message, "s", ["<node></node>"])
    return None


@unittest.skipUnless(DBUS_DAEMON, "dbus-daemon not installed")
class MprisDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert DBUS_DAEMON
        # dbus-next ferme ses flux mais pas l'objet socket de ses connexions : le ramasse-miettes s'en plaint
        warnings.filterwarnings("ignore", message="unclosed <socket", category=ResourceWarning)
        self.daemon = subprocess.Popen(
            [DBUS_DAEMON, "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.daemon.stdout is not None
        self.address = self.daemon.stdout.readline().strip()
        self.buses: list[MessageBus] = []
        self.manager_bus = await self.connect()
        self.manager = MprisManager(self.manager_bus, lambda: None)
        await self.manager.start()

    async def asyncTearDown(self) -> None:
        self.manager.stop()
        for bus in self.buses:
            bus.disconnect()
            await bus.wait_for_disconnect()
        self.daemon.terminate()
        self.daemon.wait()
        assert self.daemon.stdout is not None
        self.daemon.stdout.close()

    async def connect(self) -> MessageBus:
        bus = await MessageBus(bus_address=self.address).connect()
        self.buses.append(bus)
        return bus

    async def open_player(
        self, title: str, introspectable: bool = True, export_after: float = 0.0
    ) -> tuple[MessageBus, FakePlayer]:
        """Lecteur qui prend son nom puis annonce aussitôt ses propriétés, comme un vrai lecteur qui charge un fichier.

        ``introspectable=False`` imite Chromium et Brave : les propriétés et les méthodes répondent, mais
        Introspect renvoie un nœud vide. ``export_after`` : le nom est pris tout de suite mais les objets ne sont
        exportés que plus tard (lecteur occupé à charger un fichier)."""
        bus = await self.connect()
        if not introspectable:
            bus.add_message_handler(empty_introspection)
        player = FakePlayer(title)
        if export_after:
            await bus.request_name(NAME)
            await asyncio.sleep(export_after)
        bus.export(MPRIS_PATH, FakeRoot())
        bus.export(MPRIS_PATH, player)
        if not export_after:
            await bus.request_name(NAME)
        player.emit_properties_changed({"PlaybackStatus": "Playing"})
        return bus, player

    async def titles(self) -> list[str]:
        snapshot: dict[str, Any] = await self.manager.snapshot()
        return [player["metadata"].get("title") for player in snapshot["players"]]

    async def until(self, condition: Callable[[], Awaitable[bool]], seconds: float = 4.0) -> bool:
        for _ in range(int(seconds / 0.1)):
            if await condition():
                return True
            await asyncio.sleep(0.1)
        return await condition()

    async def test_player_appearing_after_start_is_listed(self) -> None:
        await self.open_player("first")
        self.assertTrue(await self.until(self.has_title("first")))

    async def test_player_restarted_under_the_same_name_is_listed_again(self) -> None:
        # Fermé puis relancé : l'ancien objet ne doit ni rester abonné ni annuler l'ajout du nouveau
        bus, _player = await self.open_player("first")
        self.assertTrue(await self.until(self.has_title("first")))
        bus.disconnect()
        self.assertTrue(await self.until(self.is_empty))
        for round_number in range(3):
            title = f"second-{round_number}"
            bus, _player = await self.open_player(title)
            self.assertTrue(await self.until(self.has_title(title)), f"player not listed on restart {round_number}")
            bus.disconnect()
            self.assertTrue(await self.until(self.is_empty))

    async def test_changes_of_the_restarted_player_are_followed(self) -> None:
        bus, _player = await self.open_player("first")
        self.assertTrue(await self.until(self.has_title("first")))
        bus.disconnect()
        self.assertTrue(await self.until(self.is_empty))
        _bus, player = await self.open_player("second")
        self.assertTrue(await self.until(self.has_title("second")))
        player.title = "third"
        player.emit_properties_changed({"Metadata": {"xesam:title": Variant("s", "third")}})
        self.assertTrue(await self.until(self.has_title("third")))

    async def test_player_with_empty_introspection_is_listed_and_controlled(self) -> None:
        # Chromium et Brave n'exposent aucune introspection : il faut décrire les interfaces MPRIS soi-même
        _bus, _player = await self.open_player("browser", introspectable=False)
        self.assertTrue(await self.until(self.has_title("browser")))
        snapshot: dict[str, Any] = await self.manager.snapshot()
        name = snapshot["players"][0]["name"]
        await self.manager.get(name).execute("play_pause", None)  # ne doit pas lever d'erreur

    def silence_expected_errors(self) -> None:
        """Les appels reçus avant l'export des objets sont journalisés par dbus-next côté faux lecteur : attendu ici."""
        logging.disable(logging.ERROR)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def test_player_ready_long_after_taking_its_name_is_listed(self) -> None:
        # Celluloid qui ouvre un fichier lu par le réseau : le nom est pris, les objets suivent bien plus tard
        self.silence_expected_errors()
        with (
            mock.patch.object(mpris, "ATTACH_PAUSES", (0.05, 0.05)),
            mock.patch.object(mpris, "RETRY_EVERY", 0.1),
        ):
            _bus, _player = await self.open_player("slow", export_after=1.0)
            self.assertTrue(await self.until(self.has_title("slow")))

    async def test_signal_during_an_attach_attempt_does_not_cancel_the_attach(self) -> None:
        # Une tentative est en cours (lecture initiale lente) quand le lecteur annonce un changement : ce signal ne doit
        # pas remplacer l'ajout en attente, sinon le lecteur n'est jamais listé et plus aucune tentative n'a lieu
        gate = asyncio.Event()
        original = mpris.Player.refresh
        calls = 0

        async def slow_first_refresh(player: mpris.Player) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await gate.wait()
            await original(player)

        with mock.patch.object(mpris.Player, "refresh", slow_first_refresh):
            _bus, player = await self.open_player("busy")
            self.assertTrue(await self.until(self.attempt_started(lambda: calls)))
            player.emit_properties_changed({"PlaybackStatus": "Playing"})
            await asyncio.sleep(0.3)  # laisse le signal arriver jusqu'au gestionnaire
            gate.set()
            self.assertTrue(await self.until(self.has_title("busy")))

    def attempt_started(self, count: Callable[[], int]) -> Callable[[], Awaitable[bool]]:
        async def check() -> bool:
            return count() > 0

        return check

    async def test_waiting_for_a_player_stops_when_it_leaves_the_bus(self) -> None:
        self.silence_expected_errors()
        with (
            mock.patch.object(mpris, "ATTACH_PAUSES", (0.05,)),
            mock.patch.object(mpris, "RETRY_EVERY", 0.05),
        ):
            bus = await self.connect()
            await bus.request_name(NAME)  # prend son nom, n'exporte jamais rien
            self.assertTrue(await self.until(self.is_waiting))
            bus.disconnect()
            self.assertTrue(await self.until(self.is_idle))

    async def is_waiting(self) -> bool:
        return bool(self.manager._pending)  # noqa: SLF001 - tâche de rattachement en attente

    async def is_idle(self) -> bool:
        return not self.manager._pending  # noqa: SLF001

    def has_title(self, title: str) -> Callable[[], Awaitable[bool]]:
        async def check() -> bool:
            return await self.titles() == [title]

        return check

    async def is_empty(self) -> bool:
        return await self.titles() == []


if __name__ == "__main__":
    unittest.main()

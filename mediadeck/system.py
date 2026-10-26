"""Actions système : alimentation (logind), verrouillage (ScreenSaver) et lanceurs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from dbus_next.constants import BusType
from dbus_next.aio.message_bus import MessageBus

from .launchers import LauncherStore, build_argv, build_open_argv
from .media import MediaLibrary

log = logging.getLogger(__name__)

LOGIND_NAME = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_IFACE = "org.freedesktop.login1.Manager"

# Méthodes logind : l'argument booléen « interactive » vaut False (pas de prompt polkit)
_POWER_METHODS = {"suspend": "call_suspend", "reboot": "call_reboot", "poweroff": "call_power_off"}


class SystemControl:
    def __init__(
        self,
        session_bus: MessageBus,
        launchers: LauncherStore,
        media: MediaLibrary,
        enable_power: bool,
        admin_enabled: bool,
        on_change: Callable[[], None],
    ) -> None:
        self._session_bus = session_bus
        self._launchers = launchers
        self._media = media
        self._on_change = on_change
        self._enable_power = enable_power
        self._admin_enabled = admin_enabled
        self._logind: Any = None
        self._processes: set[asyncio.subprocess.Process] = set()

    async def start(self) -> None:
        if not self._enable_power:
            return
        try:
            system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await system_bus.introspect(LOGIND_NAME, LOGIND_PATH)
            proxy = system_bus.get_proxy_object(LOGIND_NAME, LOGIND_PATH, introspection)
            self._logind = proxy.get_interface(LOGIND_IFACE)
        except Exception as exc:  # noqa: BLE001 - pas de bus système (conteneur, etc.)
            log.warning("logind unavailable, power actions disabled: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        return {
            "power": self._logind is not None,
            "admin": self._admin_enabled,
            "launchers": [
                {"id": launcher.id, "label": launcher.label, "icon": launcher.icon}
                for launcher in self._launchers.list()
            ],
            "media": self._media.snapshot(),
        }

    async def execute(self, action: str, payload: dict[str, Any]) -> None:
        match action:
            case "power":
                await self._power(payload.get("kind"))
            case "lock":
                await self._lock()
            case "launch":
                await self._launch(payload.get("id"))
            case "open_media":
                await self._open_media(payload.get("root"), payload.get("path"))
            case _:
                raise ValueError(f"unknown system action '{action}'")

    async def _power(self, kind: Any) -> None:
        if self._logind is None:
            raise RuntimeError("power actions unavailable")
        if kind not in _POWER_METHODS:
            raise ValueError("unknown power action")
        log.info("power action requested: %s", kind)
        await getattr(self._logind, _POWER_METHODS[kind])(False)

    async def _lock(self) -> None:
        # Interface freedesktop implémentée par KDE (ksmserver) et GNOME
        introspection = await self._session_bus.introspect("org.freedesktop.ScreenSaver", "/ScreenSaver")
        proxy = self._session_bus.get_proxy_object("org.freedesktop.ScreenSaver", "/ScreenSaver", introspection)
        screensaver: Any = proxy.get_interface("org.freedesktop.ScreenSaver")
        await screensaver.call_lock()

    async def _launch(self, launcher_id: Any) -> None:
        launcher = self._launchers.get(launcher_id) if isinstance(launcher_id, str) else None
        if launcher is None:
            raise ValueError("unknown launcher")
        command = build_argv(launcher)
        log.info("launching %s: %s", launcher.id, command)
        await self._spawn(command)

    async def _open_media(self, root: Any, path: Any) -> None:
        """Ouvre un fichier de l'onglet Médias avec le lecteur de son type ; le chemin est résolu côté serveur."""
        located = await asyncio.to_thread(self._media.resolve_file, root, path)
        opener = self._launchers.openers()[located.type]
        command = build_open_argv(opener, located.real)
        log.info("opening %s file with %s: %s", located.type, opener.kind, command)
        await self._spawn(command)
        # L'onglet Lecture montre le titre et l'image du dernier fichier ouvert, même sans lecteur MPRIS
        self._media.remember(located)
        self._on_change()

    async def _spawn(self, command: list[str]) -> None:
        # Session séparée : le programme survit à un redémarrage du service
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # Récupère le code de retour pour éviter les zombies
        self._processes.add(process)
        task = asyncio.create_task(process.wait())
        task.add_done_callback(lambda _t: self._processes.discard(process))

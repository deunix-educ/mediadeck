"""Suivi et pilotage des lecteurs MPRIS sur le bus D-Bus de session."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
from collections.abc import Callable
from typing import Any

from dbus_next.signature import Variant
from dbus_next.aio.message_bus import MessageBus
from dbus_next.introspection import Node

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Champs de métadonnées transmis au client
_METADATA_KEYS = {
    "mpris:trackid": "trackid",
    "mpris:length": "length",
    "mpris:artUrl": "art_url",
    "xesam:title": "title",
    "xesam:artist": "artist",
    "xesam:album": "album",
    "xesam:url": "url",
}

_LOOP_VALUES = ("None", "Track", "Playlist")

# Description des interfaces MPRIS (spécification MPRIS 2), utilisée à la place de l'introspection du lecteur :
# Chromium et Brave répondent aux méthodes et aux propriétés mais renvoient une introspection vide, ce qui empêchait
# dbus-next de construire un proxy (« interface not found on this object »).
_INTROSPECTION = Node.parse("""<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="out" type="v"/></method>
    <method name="Set"><arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="in" type="v"/></method>
    <method name="GetAll"><arg direction="in" type="s"/><arg direction="out" type="a{sv}"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" type="x" name="Offset"/></method>
    <method name="SetPosition"><arg direction="in" type="o" name="TrackId"/><arg direction="in" type="x" name="Position"/></method>
    <method name="OpenUri"><arg direction="in" type="s" name="Uri"/></method>
    <signal name="Seeked"><arg type="x" name="Position"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>""")

# Un lecteur peut prendre son nom de bus bien avant de répondre (objets exportés plus tard, ou boucle occupée à charger
# un fichier lu par le réseau) : pauses avant les premières tentatives de rattachement, puis une tentative toutes
# les RETRY_EVERY secondes tant qu'il reste sur le bus. Chaque tentative a un délai maximal.
ATTACH_PAUSES = (0.3, 0.5, 1.0, 2.0, 4.0)
RETRY_EVERY = 3.0
ATTACH_TIMEOUT = 5.0


def unwrap(value: Any) -> Any:
    """Convertit récursivement les Variant D-Bus en types Python natifs."""
    if isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {key: unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unwrap(item) for item in value]
    return value


def normalize_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Réduit les métadonnées MPRIS aux champs utiles, avec des types stables."""
    data = unwrap(raw)
    result: dict[str, Any] = {}
    for source, target in _METADATA_KEYS.items():
        if source in data:
            result[target] = data[source]
    # xesam:artist est une liste de chaînes selon la spec
    artist = result.get("artist")
    if isinstance(artist, list):
        result["artist"] = ", ".join(str(item) for item in artist)
    # mpris:length peut être int64 ou uint64 selon le lecteur
    length = result.get("length")
    result["length"] = int(length) if isinstance(length, (int, float)) else 0
    return result


class Player:
    """Un lecteur MPRIS et son état mis en cache."""

    def __init__(self, bus_name: str, root: Any, player: Any, props: Any) -> None:
        self.bus_name = bus_name
        self._root = root
        self._player = player
        self._props = props
        self.state: dict[str, Any] = {}
        self.position_us = 0
        self.position_stamp = 0.0
        self._unsubscribers: list[Callable[[], None]] = []

    def on_close(self, unsubscribe: Callable[[], None]) -> None:
        """Enregistre le désabonnement d'un signal, exécuté par close()."""
        self._unsubscribers.append(unsubscribe)

    def close(self) -> None:
        """Se désabonne des signaux : un lecteur relancé sous le même nom ne doit pas réveiller l'ancien objet."""
        unsubscribers, self._unsubscribers = self._unsubscribers, []
        for unsubscribe in unsubscribers:
            try:
                unsubscribe()
            except Exception as exc:  # noqa: BLE001 - le bus peut déjà être fermé
                log.debug("cannot unsubscribe from %s: %s", self.bus_name, exc)

    @property
    def art_url(self) -> str | None:
        url = self.state.get("metadata", {}).get("art_url")
        return url if isinstance(url, str) and url else None

    async def refresh(self) -> None:
        """Relit toutes les propriétés (appelé à l'ajout et sur PropertiesChanged)."""
        root_props = unwrap(await self._props.call_get_all(ROOT_IFACE))
        player_props = unwrap(await self._props.call_get_all(PLAYER_IFACE))
        self.state = {
            "identity": root_props.get("Identity", self.bus_name.removeprefix(MPRIS_PREFIX)),
            "can_raise": bool(root_props.get("CanRaise", False)),
            "status": player_props.get("PlaybackStatus", "Stopped"),
            "metadata": normalize_metadata(player_props.get("Metadata", {})),
            "volume": player_props.get("Volume"),
            "shuffle": player_props.get("Shuffle"),
            "loop": player_props.get("LoopStatus"),
            "can_play": bool(player_props.get("CanPlay", False)),
            "can_pause": bool(player_props.get("CanPause", False)),
            "can_seek": bool(player_props.get("CanSeek", False)),
            "can_next": bool(player_props.get("CanGoNext", False)),
            "can_previous": bool(player_props.get("CanGoPrevious", False)),
        }
        await self.refresh_position()

    async def refresh_position(self) -> None:
        """Position non signalée par PropertiesChanged : lecture explicite."""
        try:
            value = unwrap(await self._props.call_get(PLAYER_IFACE, "Position"))
            self.position_us = int(value)
        except Exception:  # noqa: BLE001 - certains lecteurs ne l'exposent pas
            self.position_us = 0
        self.position_stamp = asyncio.get_running_loop().time()

    def snapshot(self, art_path: str | None) -> dict[str, Any]:
        metadata = dict(self.state.get("metadata", {}))
        # L'URL de pochette réelle reste côté serveur (file:// notamment)
        metadata.pop("art_url", None)
        metadata["art"] = art_path
        return {**self.state, "name": self.bus_name, "metadata": metadata, "position": self.position_us}

    async def execute(self, action: str, value: Any) -> None:
        """Exécute une commande validée sur le lecteur."""
        player = self._player
        match action:
            case "play_pause":
                await player.call_play_pause()
            case "play":
                await player.call_play()
            case "pause":
                await player.call_pause()
            case "stop":
                await player.call_stop()
            case "next":
                await player.call_next()
            case "previous":
                await player.call_previous()
            case "seek":
                # Décalage relatif en microsecondes
                await player.call_seek(int(_require_number(value)))
            case "set_position":
                trackid = self.state.get("metadata", {}).get("trackid")
                if not isinstance(trackid, str) or not trackid.startswith("/"):
                    raise ValueError("player does not expose a valid trackid")
                await player.call_set_position(trackid, max(0, int(_require_number(value))))
            case "set_volume":
                volume = min(1.0, max(0.0, float(_require_number(value))))
                await self._props.call_set(PLAYER_IFACE, "Volume", Variant("d", volume))
            case "shuffle":
                if not isinstance(value, bool):
                    raise ValueError("shuffle expects a boolean")
                await self._props.call_set(PLAYER_IFACE, "Shuffle", Variant("b", value))
            case "loop":
                if value not in _LOOP_VALUES:
                    raise ValueError(f"loop expects one of {_LOOP_VALUES}")
                await self._props.call_set(PLAYER_IFACE, "LoopStatus", Variant("s", value))
            case "raise":
                await self._root.call_raise()
            case _:
                raise ValueError(f"unknown player action '{action}'")
        if action in {"seek", "set_position"}:
            await self.refresh_position()


def _require_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric value expected")
    return float(value)


class MprisManager:
    """Découvre les lecteurs, suit leurs changements et notifie le serveur."""

    def __init__(self, bus: MessageBus, on_change: Callable[[], None]) -> None:
        self._bus = bus
        self._on_change = on_change
        self._players: dict[str, Player] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        introspection = await self._bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        proxy = self._bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspection)
        dbus: Any = proxy.get_interface("org.freedesktop.DBus")
        dbus.on_name_owner_changed(self._on_name_owner_changed)
        for name in await dbus.call_list_names():
            if name.startswith(MPRIS_PREFIX):
                await self._add(name)

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        if not name.startswith(MPRIS_PREFIX):
            return
        if old_owner:
            # Le lecteur a quitté le bus : plus rien à rattacher (ajout en attente) ni à suivre
            pending = self._pending.pop(name, None)
            if pending is not None:
                pending.cancel()
            if name in self._players:
                self._remove(name)
        if new_owner:
            # Laisse le lecteur publier son objet avant l'introspection
            self._schedule(name, self._add(name, delay=0.3))

    def _schedule(self, name: str, coro: Any) -> None:
        previous = self._pending.pop(name, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(coro)
        self._pending[name] = task

        def on_done(done: asyncio.Task[None]) -> None:
            # Ne retire que la tâche courante, pas une remplaçante plus récente
            if self._pending.get(name) is done:
                del self._pending[name]

        task.add_done_callback(on_done)

    async def _add(self, name: str, delay: float = 0.0) -> None:
        """Rattache un lecteur, en réessayant tant qu'il reste sur le bus (la tâche est annulée à son départ)."""
        pauses = itertools.chain((delay,), ATTACH_PAUSES, itertools.repeat(RETRY_EVERY))
        attempts = 0
        for pause in pauses:
            if pause:
                await asyncio.sleep(pause)
            attempts += 1
            try:
                player = await asyncio.wait_for(self._attach(name), ATTACH_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - lecteur au D-Bus défaillant ou pas encore prêt
                log.debug("MPRIS player %s not ready (attempt %d): %s", name, attempts, exc)
                if attempts == len(ATTACH_PAUSES) + 1:
                    log.warning("MPRIS player %s does not answer yet (%r), still trying", name, exc)
                continue
            self._players[name] = player
            log.info("MPRIS player added: %s", name)
            self._on_change()
            return

    async def _attach(self, name: str) -> Player:
        proxy = self._bus.get_proxy_object(name, MPRIS_PATH, _INTROSPECTION)
        root: Any = proxy.get_interface(ROOT_IFACE)
        player_iface: Any = proxy.get_interface(PLAYER_IFACE)
        props: Any = proxy.get_interface(PROPS_IFACE)
        player = Player(name, root, player_iface, props)

        def on_properties_changed(interface: str, _changed: dict[str, Any], _invalidated: list[str]) -> None:
            # Un lecteur pas encore inscrit est en cours de rattachement : son ajout en attente (la tâche qui exécute
            # cette tentative) ne doit pas être remplacé, et sa lecture initiale verra déjà ce changement
            if interface in (PLAYER_IFACE, ROOT_IFACE) and self._players.get(name) is player:
                self._schedule(name, self._refresh(player))

        def on_seeked(position: int) -> None:
            player.position_us = int(position)
            player.position_stamp = asyncio.get_running_loop().time()
            self._on_change()

        props.on_properties_changed(on_properties_changed)
        player.on_close(lambda: props.off_properties_changed(on_properties_changed))
        player_iface.on_seeked(on_seeked)
        player.on_close(lambda: player_iface.off_seeked(on_seeked))
        try:
            await player.refresh()
        except BaseException:
            player.close()
            raise
        return player

    async def _refresh(self, player: Player) -> None:
        # Regroupe les rafales de PropertiesChanged
        await asyncio.sleep(0.05)
        try:
            await player.refresh()
        except Exception as exc:  # noqa: BLE001
            log.debug("refresh failed for %s: %s", player.bus_name, exc)
            return
        self._on_change()

    def stop(self) -> None:
        """Annule les rattachements en attente et se désabonne de tous les lecteurs."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        for player in self._players.values():
            player.close()
        self._players.clear()

    def _remove(self, name: str) -> None:
        player = self._players.pop(name, None)
        if player is not None:
            player.close()
        log.info("MPRIS player removed: %s", name)
        self._on_change()

    def get(self, name: str) -> Player:
        player = self._players.get(name)
        if player is None:
            raise ValueError(f"unknown player '{name}'")
        return player

    def active_name(self) -> str | None:
        """Lecteur par défaut : celui en lecture, sinon en pause, sinon le premier."""
        for status in ("Playing", "Paused"):
            for name, player in self._players.items():
                if player.state.get("status") == status:
                    return name
        return next(iter(self._players), None)

    async def snapshot(self) -> dict[str, Any]:
        players = []
        for player in list(self._players.values()):
            if player.state.get("status") == "Playing":
                await player.refresh_position()
            players.append(player.snapshot(art_path(player)))
        return {"players": players, "active": self.active_name()}


def art_path(player: Player) -> str | None:
    """Chemin exposé au client pour la pochette.

    Les pochettes locales (file://) passent par /art ; les URL http(s) sont
    servies directement au client ; les autres schémas sont ignorés.
    """
    url = player.art_url
    if url is None:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("file://"):
        # Le hash force le rechargement du cache navigateur à chaque changement
        digest = hashlib.sha1(url.encode()).hexdigest()[:12]
        return f"/art?player={player.bus_name}&v={digest}"
    return None

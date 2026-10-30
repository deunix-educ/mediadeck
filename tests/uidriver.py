"""Pilote minimal de Chromium (protocole DevTools) et faux serveur de la vraie interface, pour les tests de mise en page."""

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from mediadeck.server import render_page, versioned_static

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

PLAYER: dict[str, Any] = {
    "name": "org.mpris.MediaPlayer2.test", "identity": "Lecteur de test", "can_raise": True, "status": "Playing",
    "metadata": {"title": "Concerto pour piano n°21 en do majeur, K. 467 – II. Andante", "artist": "Wolfgang Amadeus Mozart",
                 "album": "Les plus grands concertos", "length": 412_000_000, "art": None},
    "volume": 0.8, "shuffle": False, "loop": "None", "can_play": True, "can_pause": True, "can_seek": True,
    "can_next": True, "can_previous": True, "position": 131_000_000,
}
SINKS = [{"name": "hdmi", "description": "Sortie HDMI / DisplayPort", "volume": 0.62, "muted": False}]
LAST = {"name": "Un très long nom de fichier vidéo qui dépasse largement une ligne - 1080p", "type": "video", "art": None}


def state(scenario: str) -> dict[str, Any]:
    players = [] if scenario == "last" else [PLAYER]
    media = {"roots": [{"id": "videos", "name": "Videos"}], "thumbnails": False, "last": LAST if scenario == "last" else None}
    return {
        "type": "state", "version": "test",
        "mpris": {"players": players, "active": PLAYER["name"] if players else None, "available": True},
        "audio": {"available": True, "default": "hdmi", "max_volume": 1.0, "sinks": SINKS},
        "input": {"available": True, "reason": None, "keys": [],
                  "text": {"available": False, "reason": "disabled in config (features.text_input)"}},
        "system": {"power": True, "admin": True, "launchers": [], "media": media},
    }


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    socket_ = web.WebSocketResponse()
    await socket_.prepare(request)
    # La page ne transmet que le jeton : il porte ici le nom du scénario (« player » ou « last »)
    await socket_.send_json(state(request.query.get("token", "player")))
    async for message in socket_:
        if message.type == WSMsgType.ERROR:
            break
    return socket_


async def _index(_request: web.Request) -> web.Response:
    return render_page("index.html")


def make_app() -> web.Application:
    """La vraie page et les vrais fichiers statiques (mêmes fonctions que le serveur), avec un état de démonstration."""
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _websocket)
    app.router.add_get("/static/{version}/{path:.+}", versioned_static)
    return app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Browser:
    """Chromium sans interface, piloté par DevTools."""

    def __init__(self) -> None:
        assert CHROMIUM
        self._profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._port = free_port()
        self._process: subprocess.Popen[bytes] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self.exceptions: list[str] = []  # exceptions JavaScript non rattrapées, signalées par la page

    async def start(self) -> None:
        assert CHROMIUM
        self._process = subprocess.Popen(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--password-store=basic",
             f"--remote-debugging-port={self._port}", f"--user-data-dir={self._profile.name}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # Aucun bus D-Bus : sinon Chromium hériterait du bus de test (ou de celui de l'utilisateur) et y ferait
            # activer des services réels (portails, kwallet, kdeconnect…) qui survivent au test
            env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"},
            start_new_session=True,  # groupe à part : close() termine aussi les processus enfants de Chromium
        )
        self._session = aiohttp.ClientSession()
        for _ in range(100):
            try:
                async with self._session.get(f"http://127.0.0.1:{self._port}/json") as response:
                    pages = [page for page in await response.json() if page["type"] == "page"]
                    if pages:
                        self._socket = await self._session.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0)
                        await self.call("Runtime.enable")  # active Runtime.exceptionThrown
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError("Chromium did not start")

    def signal_group(self, number: signal.Signals) -> None:
        assert self._process
        try:
            os.killpg(self._process.pid, number)
        except ProcessLookupError:
            pass

    async def close(self) -> None:
        if self._socket:
            await self._socket.close()
        if self._session:
            await self._session.close()
        if self._process:
            # Les processus enfants de Chromium écrivent encore dans le profil : on termine tout le groupe avant de le supprimer
            self.signal_group(signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.signal_group(signal.SIGKILL)  # enfants restants
            self._process.wait(timeout=10)
        self._profile.cleanup()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        assert self._socket
        self._next_id += 1
        await self._socket.send_json({"id": self._next_id, "method": method, "params": params})
        while True:
            message = json.loads((await self._socket.receive()).data)
            if message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.exceptions.append(details.get("exception", {}).get("description") or details.get("text", "?"))
            if message.get("id") == self._next_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    async def open(self, url: str, width: int, height: int) -> None:
        await self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=height > width)
        await self.call("Page.navigate", url=url)

    async def evaluate(self, expression: str) -> Any:
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def wait_for(self, expression: str, timeout: float = 8.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                if await self.evaluate(expression):
                    return
            except RuntimeError:
                pass  # page pas encore chargée (élément absent) : on réessaie jusqu'au délai
            await asyncio.sleep(0.1)
        raise TimeoutError(expression)

"""Serveur HTTP + WebSocket : diffuse l'état et relaie les commandes de la tablette."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import signal
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from aiohttp import WSMsgType, web
from dbus_next.aio.message_bus import MessageBus

from . import __version__
from .admin import AdminApi
from .audio import AudioManager
from .config import Config
from .input import VirtualInput
from .keymap import LayoutSpec, current_layout
from .launchers import LauncherStore
from .media import MediaApi, MediaLibrary
from .mpris import MprisManager
from .system import SystemControl
from .text import TextTyper
from .thumbs import Thumbnailer

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_DIR = WEB_DIR / "static"
MAX_ART_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 4096
STATE_REFRESH_SECONDS = 10.0

CSP = (
    "default-src 'self'; img-src 'self' https: http: data:; "
    "connect-src 'self' ws: wss:; style-src 'self'; script-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def static_version(directory: Path = STATIC_DIR) -> str:
    """Empreinte des fichiers statiques (chemin, taille, date) : change dès qu'un fichier change."""
    digest = hashlib.sha1()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path.relative_to(directory)}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:10]


def render_page(name: str) -> web.Response:
    """Page HTML dont les fichiers statiques pointent vers /static/<version>/… : après une mise à jour, la page
    (toujours revalidée) fait charger de nouvelles URL, donc jamais un ancien script resté dans le cache."""
    version = static_version()
    html = (WEB_DIR / name).read_text(encoding="utf-8").replace('"/static/', f'"/static/{version}/')
    return web.Response(text=html, content_type="text/html")


async def versioned_static(request: web.Request) -> web.StreamResponse:
    """/static/<version>/<fichier> : la version n'est que dans l'URL, ce qui permet un cache immuable."""
    target = (STATIC_DIR / request.match_info["path"]).resolve()
    if STATIC_DIR.resolve() not in target.parents or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


class MediaDeckServer:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._clients: set[web.WebSocketResponse] = set()
        self._broadcast_task: asyncio.Task[None] | None = None
        self._background: list[asyncio.Task[None]] = []
        self._bus: MessageBus | None = None
        self.mpris: MprisManager | None = None
        self.audio = AudioManager(config.max_volume, self.notify)
        self.input = VirtualInput(config.enable_input)
        self.text = TextTyper(self.input, config.enable_text_input, self._keyboard_layout)
        self.system: SystemControl | None = None
        self.launchers = LauncherStore()
        self.thumbnailer = Thumbnailer() if config.media_thumbnails else None
        self.media = MediaLibrary(
            config.media_roots,
            thumbnails=self.thumbnailer is not None and self.thumbnailer.available,
            follow_symlinks=config.media_follow_symlinks,
        )
        if config.media_thumbnails and not self.media.thumbnails:
            log.info("ffmpeg not found: no thumbnails (apt install ffmpeg)")
        for line in self.media.describe():
            log.info("media folder %s", line)
        imported = self.launchers.seed(config.launchers)
        if imported:
            log.info("imported %d launcher(s) from the config file; manage them on /admin", imported)
        elif config.launchers:
            log.info("[[launchers]] of the config file are ignored (already imported): manage them on /admin")

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def on_startup(self, _app: web.Application) -> None:
        try:
            self._bus = await MessageBus().connect()
            self.mpris = MprisManager(self._bus, self.notify)
            await self.mpris.start()
            self.system = SystemControl(
                self._bus,
                self.launchers,
                self.media,
                self._config.enable_power,
                self._config.admin_enabled,
                self.notify,
            )
            await self.system.start()
        except Exception as exc:  # noqa: BLE001 - lancé hors session graphique
            log.error("D-Bus session bus unavailable, MPRIS and system control disabled: %s", exc)
        await self.audio.start()
        self._background.append(asyncio.create_task(self._periodic_state()))

    async def on_shutdown(self, _app: web.Application) -> None:
        for task in self._background:
            task.cancel()
        await self.audio.stop()
        for client in list(self._clients):
            await client.close(code=1001, message=b"server shutdown")
        self.text.close()
        self.input.close()
        self.launchers.close()
        if self.mpris is not None:
            self.mpris.stop()
        if self._bus is not None:
            self._bus.disconnect()

    async def _keyboard_layout(self) -> LayoutSpec:
        # Disposition active du PC, relue à chaque saisie (KDE peut basculer AZERTY/QWERTY)
        return await current_layout(self._bus)

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[security_headers])
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.websocket)
        app.router.add_get("/art", self.art)
        if self._config.admin_enabled:
            app.router.add_get("/admin", self.admin_page)
            AdminApi(self._config, self.launchers, self.notify).add_routes(app)
        if self.media.roots:
            MediaApi(self._config, self.media, self.thumbnailer).add_routes(app)
        if self._config.tls_ca is not None:
            app.router.add_get("/ca.crt", self.ca_certificate)
        app.router.add_get("/static/{version}/{path:.+}", versioned_static)
        # Anciennes adresses (page restée dans un cache) : toujours servies, mais revalidées
        app.router.add_static("/static", STATIC_DIR, show_index=False, follow_symlinks=False)
        return app

    # ------------------------------------------------------------------
    # Authentification
    # ------------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        token = request.query.get("token", "")
        return hmac.compare_digest(token.encode(), self._config.token.encode())

    # ------------------------------------------------------------------
    # Routes HTTP
    # ------------------------------------------------------------------

    async def index(self, _request: web.Request) -> web.StreamResponse:
        # La page elle-même est publique : le jeton protège /ws et /art
        return render_page("index.html")

    async def admin_page(self, _request: web.Request) -> web.StreamResponse:
        # Page publique comme l'index : l'API /api/* exige le jeton (en-tête Authorization)
        return render_page("admin.html")

    async def ca_certificate(self, _request: web.Request) -> web.StreamResponse:
        """Certificat public de la CA locale, à installer une fois sur la tablette."""
        ca = self._config.tls_ca
        if ca is None or not ca.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(
            ca,
            headers={
                "Content-Type": "application/x-x509-ca-cert",
                "Content-Disposition": 'attachment; filename="mediadeck-ca.crt"',
            },
        )

    async def art(self, request: web.Request) -> web.StreamResponse:
        """Sert la pochette locale (file://) du lecteur demandé, et rien d'autre."""
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        if self.mpris is None:
            raise web.HTTPNotFound()
        try:
            player = self.mpris.get(request.query.get("player", ""))
        except ValueError as exc:
            raise web.HTTPNotFound() from exc
        url = player.art_url
        if url is None or not url.startswith("file://"):
            raise web.HTTPNotFound()
        path = Path(unquote(urlparse(url).path))
        mime, _ = mimetypes.guess_type(path.name)
        # Seuls les fichiers image réguliers sont servis
        if mime is None or not mime.startswith("image/") or not path.is_file():
            raise web.HTTPNotFound()
        if path.stat().st_size > MAX_ART_BYTES:
            raise web.HTTPRequestEntityTooLarge(MAX_ART_BYTES, path.stat().st_size)
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("client connected: %s (%d total)", request.remote, len(self._clients))
        try:
            await ws.send_json(await self.state())
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._handle(ws, message.data)
                elif message.type == WSMsgType.ERROR:
                    log.debug("websocket error: %s", ws.exception())
        finally:
            self._clients.discard(ws)
            if self.input.release_owned_by(ws):
                log.info("released the mouse button(s) held by %s", request.remote)
                self.notify()
            log.info("client disconnected: %s", request.remote)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            await self._dispatch(message, ws)
        except Exception as exc:  # noqa: BLE001 - toute erreur est renvoyée au client
            log.debug("command failed: %s", exc)
            await ws.send_json({"type": "error", "message": str(exc)})

    async def _dispatch(self, message: dict[str, Any], owner: object | None = None) -> None:
        domain = message.get("domain")
        action = message.get("action")
        if not isinstance(action, str):
            raise ValueError("missing action")
        match domain:
            case "player":
                if self.mpris is None:
                    raise RuntimeError("MPRIS unavailable")
                player_name = message.get("player")
                if not isinstance(player_name, str):
                    raise ValueError("missing player")
                await self.mpris.get(player_name).execute(action, message.get("value"))
            case "audio":
                await self.audio.execute(action, message.get("sink"), message.get("value"))
            case "input":
                if action == "text":
                    await self.text.type(message.get("text"))
                else:
                    self.input.execute(action, message, owner)
                    if action == "button":
                        self.notify()  # tous les clients voient les boutons maintenus
            case "system":
                if self.system is None:
                    raise RuntimeError("system control unavailable")
                await self.system.execute(action, message)
            case "ping":
                return
            case _:
                raise ValueError(f"unknown domain '{domain}'")

    # ------------------------------------------------------------------
    # Diffusion de l'état
    # ------------------------------------------------------------------

    async def state(self) -> dict[str, Any]:
        mpris_state: dict[str, Any] = {"players": [], "active": None, "available": False}
        if self.mpris is not None:
            mpris_state = {**await self.mpris.snapshot(), "available": True}
            for player in mpris_state["players"]:
                self.media.enrich(player["metadata"])  # titre et image d'un fichier de la bibliothèque
        system_state = (
            self.system.snapshot()
            if self.system
            else {"power": False, "admin": self._config.admin_enabled, "launchers": [], "media": self.media.snapshot()}
        )
        return {
            "type": "state",
            "version": __version__,
            "mpris": mpris_state,
            "audio": self.audio.snapshot(),
            "input": {**self.input.snapshot(), "text": self.text.snapshot()},
            "system": system_state,
        }

    def notify(self) -> None:
        """Planifie une diffusion groupée (anti-rebond 100 ms)."""
        if not self._clients:
            return
        if self._broadcast_task is not None and not self._broadcast_task.done():
            return
        self._broadcast_task = asyncio.create_task(self._broadcast(delay=0.1))

    async def _broadcast(self, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        if not self._clients:
            return
        payload = json.dumps(await self.state())
        for client in list(self._clients):
            try:
                await client.send_str(payload)
            except ConnectionResetError:
                self._clients.discard(client)

    async def _periodic_state(self) -> None:
        # Recale régulièrement la position de lecture interpolée côté client
        while True:
            await asyncio.sleep(STATE_REFRESH_SECONDS)
            await self._broadcast()


async def open_sites(app: web.Application, config: Config, ssl_context: ssl.SSLContext | None) -> web.AppRunner:
    """Ouvre le port principal (HTTPS si un contexte TLS est fourni) et, si demandé, le port HTTP en clair."""
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, config.host, config.port, ssl_context=ssl_context).start()
        if config.http_port:
            await web.TCPSite(runner, config.host, config.http_port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


async def serve(app: web.Application, config: Config, ssl_context: ssl.SSLContext | None) -> None:
    """Sert l'application jusqu'à SIGINT / SIGTERM, puis arrête proprement (on_shutdown)."""
    runner = await open_sites(app, config, ssl_context)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


@web.middleware
async def security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    response: web.StreamResponse = await handler(request)
    # Pages et fichiers statiques : revalidés à chaque chargement (ETag, réponse 304), sinon une tablette garde
    # de vieux scripts pendant des heures après une mise à jour. /art et l'API fixent déjà leur propre valeur.
    response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response

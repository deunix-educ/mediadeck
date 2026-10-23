"""API REST d'administration des lanceurs (page /admin).

Authentification par ``Authorization: Bearer <jeton>`` : contrairement à un paramètre d'URL, un en-tête
personnalisé ne peut pas être ajouté par un site tiers (pas de CORS), ce qui exclut le CSRF.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .config import Config
from .launchers import (
    BROWSERS,
    ICONS,
    KINDS,
    CommandNotAllowed,
    LauncherError,
    LauncherNotFound,
    LauncherStore,
    list_desktop_apps,
    validate,
)

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024

Handler = Callable[["AdminApi", web.Request], Awaitable[web.StreamResponse]]


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _error(status: int, message: str) -> web.Response:
    return _json({"error": message}, status)


def _api(handler: Handler) -> Callable[["AdminApi", web.Request], Awaitable[web.StreamResponse]]:
    """Décorateur : authentification puis traduction des erreurs métier en réponses JSON."""

    async def wrapper(self: AdminApi, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return _error(401, "invalid or missing token")
        try:
            return await handler(self, request)
        except CommandNotAllowed as exc:
            return _error(403, str(exc))
        except LauncherNotFound as exc:
            return _error(404, str(exc))
        except LauncherError as exc:
            return _error(400, str(exc))

    return wrapper


class AdminApi:
    def __init__(self, config: Config, store: LauncherStore, on_change: Callable[[], None]) -> None:
        self._config = config
        self._store = store
        self._on_change = on_change

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/launchers", self.list_launchers)
        app.router.add_post("/api/launchers", self.create)
        app.router.add_post("/api/launchers/reorder", self.reorder)
        app.router.add_put("/api/launchers/{id}", self.update)
        app.router.add_delete("/api/launchers/{id}", self.delete)
        app.router.add_get("/api/apps", self.apps)

    def authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(token.strip().encode(), self._config.token.encode())

    @staticmethod
    async def _body(request: web.Request) -> Any:
        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise LauncherError("request body too large")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise LauncherError("invalid JSON") from exc

    def _changed(self) -> None:
        self._on_change()

    @_api
    async def list_launchers(self, _request: web.Request) -> web.StreamResponse:
        return _json(
            {
                "launchers": [entry.to_dict() for entry in self._store.list()],
                "icons": list(ICONS),
                "kinds": list(KINDS),
                "browsers": [
                    {
                        "id": browser.id,
                        "name": browser.name,
                        "installed": browser.installed,
                        "fullscreen": browser.fullscreen_args is not None,
                    }
                    for browser in BROWSERS
                ],
                "allow_command": self._config.allow_custom_command,
            }
        )

    @_api
    async def apps(self, _request: web.Request) -> web.StreamResponse:
        return _json([{"id": app.id, "name": app.name} for app in list_desktop_apps()])

    @_api
    async def create(self, request: web.Request) -> web.StreamResponse:
        label, icon, kind, spec = validate(
            await self._body(request), allow_command=self._config.allow_custom_command
        )
        entry = self._store.create(label, icon, kind, spec)
        log.info("launcher created: %s (%s)", entry.id, entry.kind)
        self._changed()
        return _json(entry.to_dict(), 201)

    @_api
    async def update(self, request: web.Request) -> web.StreamResponse:
        current = self._store.get(request.match_info["id"])
        if current is None:
            raise LauncherNotFound("unknown launcher")
        label, icon, kind, spec = validate(
            await self._body(request), allow_command=self._config.allow_custom_command, current=current
        )
        entry = self._store.update(current.id, label, icon, kind, spec)
        log.info("launcher updated: %s (%s)", entry.id, entry.kind)
        self._changed()
        return _json(entry.to_dict())

    @_api
    async def delete(self, request: web.Request) -> web.StreamResponse:
        launcher_id = request.match_info["id"]
        self._store.delete(launcher_id)
        log.info("launcher deleted: %s", launcher_id)
        self._changed()
        return web.Response(status=204, headers={"Cache-Control": "no-store"})

    @_api
    async def reorder(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise LauncherError("ids must be a list of launcher ids")
        self._store.reorder(ids)
        self._changed()
        return web.Response(status=204, headers={"Cache-Control": "no-store"})

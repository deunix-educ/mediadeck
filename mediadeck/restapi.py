"""Éléments communs des API REST (/api/*) : réponses JSON et authentification par en-tête.

Le jeton est lu dans ``Authorization: Bearer <jeton>`` : contrairement à un paramètre d'URL, un en-tête
personnalisé ne peut pas être ajouté par un site tiers (pas de CORS), ce qui exclut le CSRF.
"""

from __future__ import annotations

import hmac
from typing import Any

from aiohttp import web


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def error_response(status: int, message: str) -> web.Response:
    return json_response({"error": message}, status)


def bearer_authorized(request: web.Request, token: str) -> bool:
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())

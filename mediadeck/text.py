"""Saisie de texte depuis la tablette : chaque caractère devient les touches d'un vrai clavier.

Le texte passe par le clavier virtuel uinput, traduit selon la disposition active du PC (keymap.py) : aucun
presse-papiers n'est touché, donc rien n'est conservé dans l'historique de Klipper, mots de passe compris.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .keymap import Keymap, KeymapError, LayoutSpec, Stroke, current_layout, load_xkb

log = logging.getLogger(__name__)

# 500 caractères de 4 octets (emoji) = 2 Ko, sous la limite de 4 Ko d'un message WebSocket (qui coupe la connexion)
MAX_TEXT_LENGTH = 500
# Pause entre deux phases d'une frappe, pour que le serveur graphique les voie dans l'ordre
STROKE_DELAY = 0.004
MAX_REPORTED = 10

LayoutProvider = Callable[[], Awaitable[LayoutSpec]]


class KeyWriter(Protocol):
    """Ce dont la saisie a besoin du clavier virtuel (VirtualInput, ou un faux dans les tests)."""

    @property
    def available(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    def write_keys(self, events: list[tuple[int, bool]]) -> None: ...


class TextTyper:
    def __init__(
        self,
        keyboard: KeyWriter,
        enabled: bool,
        layout: LayoutProvider | None = None,
        delay: float = STROKE_DELAY,
    ) -> None:
        self._keyboard = keyboard
        self._layout: LayoutProvider = layout or (lambda: current_layout(None))
        self._delay = delay
        self._keymaps: dict[LayoutSpec, Keymap] = {}
        self._lock = asyncio.Lock()
        self.reason = ""
        if not enabled:
            self.reason = "disabled in config (features.text_input)"
        elif not keyboard.available:
            self.reason = keyboard.reason or "virtual input unavailable"
        else:
            try:
                load_xkb()
            except KeymapError as exc:
                self.reason = str(exc)
        # Sans cette ligne, un bouton « Clavier » grisé sur la tablette n'a aucune trace côté PC
        if self.reason:
            log.info("text entry unavailable: %s", self.reason)
        else:
            log.info("text entry enabled")

    @property
    def available(self) -> bool:
        return not self.reason

    def snapshot(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}

    async def _keymap(self) -> Keymap:
        # La disposition peut changer en cours de route (bascule AZERTY/QWERTY) : relue à chaque saisie
        spec = await self._layout()
        keymap = self._keymaps.get(spec)
        if keymap is None:
            keymap = await asyncio.to_thread(Keymap, spec)
            self._keymaps[spec] = keymap
            log.info("keyboard layout for text entry: %s", spec)
        return keymap

    async def _press(self, stroke: Stroke, keymap: Keymap) -> None:
        modifiers: list[int] = []
        if stroke.shift and keymap.shift_code is not None:
            modifiers.append(keymap.shift_code)
        if stroke.altgr and keymap.altgr_code is not None:
            modifiers.append(keymap.altgr_code)
        self._keyboard.write_keys([*((code, True) for code in modifiers), (stroke.code, True)])
        try:
            await asyncio.sleep(self._delay)
        finally:
            # Même si la connexion est coupée en pleine saisie, aucune touche ne reste enfoncée
            self._keyboard.write_keys([(stroke.code, False), *((code, False) for code in reversed(modifiers))])
        await asyncio.sleep(self._delay)

    async def type(self, text: Any) -> None:
        if not self.available:
            raise RuntimeError(f"text entry unavailable: {self.reason}")
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"text too long (maximum {MAX_TEXT_LENGTH} characters)")
        # Une seule saisie à la fois : deux textes envoyés coup sur coup ne doivent pas s'entremêler
        async with self._lock:
            try:
                keymap = await self._keymap()
            except KeymapError as exc:
                raise RuntimeError(str(exc)) from exc
            strokes, skipped = keymap.plan(text)
            log.info("typing %d character(s) as %d key stroke(s)", len(text), len(strokes))
            for stroke in strokes:
                await self._press(stroke, keymap)
        if skipped:
            shown = "".join(skipped[:MAX_REPORTED])
            raise ValueError(f"characters not available on the PC keyboard layout, skipped: {shown}")

    def close(self) -> None:
        for keymap in self._keymaps.values():
            keymap.close()
        self._keymaps.clear()

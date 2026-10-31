"""Clavier et souris virtuels via /dev/uinput (fonctionne sous X11 et Wayland)."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from evdev import UInput, ecodes  # type: ignore[import-untyped]
except ImportError:  # evdev absent : l'entrée virtuelle est simplement désactivée
    UInput = None
    ecodes = None

# Touches exposées au client : noms stables -> codes evdev.
# Volontairement limité à la navigation : les caractères passent par la saisie de texte (text.py),
# qui traduit chaque caractère selon la disposition réelle du clavier du PC.
KEY_NAMES = {
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "enter": "KEY_ENTER",
    "escape": "KEY_ESC",
    "back": "KEY_BACKSPACE",
    "space": "KEY_SPACE",
    "tab": "KEY_TAB",
    "home": "KEY_HOME",
    "end": "KEY_END",
    "page_up": "KEY_PAGEUP",
    "page_down": "KEY_PAGEDOWN",
    "fullscreen": "KEY_F",
    "f11": "KEY_F11",
    "meta": "KEY_LEFTMETA",
    "play_pause": "KEY_PLAYPAUSE",
    "next": "KEY_NEXTSONG",
    "previous": "KEY_PREVIOUSSONG",
    "volume_up": "KEY_VOLUMEUP",
    "volume_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
}

BUTTONS = {"left": "BTN_LEFT", "right": "BTN_RIGHT", "middle": "BTN_MIDDLE"}

# Touches que le clavier virtuel sait produire pour la saisie de texte : bloc principal (Échap … Espace, soit les codes
# 1 à 57), touche « <> » (86) et AltGr (100). Jamais exposées au client : seules les touches de KEY_NAMES le sont.
TYPING_KEYS = frozenset(range(1, 58)) | {86, 100}

# Bornes anti-abus pour les messages du client
MAX_MOVE = 500
MAX_SCROLL = 20


class VirtualInput:
    """Deux périphériques distincts (clavier + pointeur) pour une détection libinput fiable."""

    def __init__(self, enabled: bool) -> None:
        self._keyboard: Any = None
        self._pointer: Any = None
        # Boutons de souris maintenus (bascule de l'interface) -> connexion qui les a enfoncés
        self._held: dict[str, object | None] = {}
        self.reason = ""
        if not enabled:
            self.reason = "disabled in config"
            return
        if UInput is None or ecodes is None:
            self.reason = "python3-evdev not installed"
            return
        try:
            keys = sorted({getattr(ecodes, code) for code in KEY_NAMES.values()} | TYPING_KEYS)
            self._keyboard = UInput({ecodes.EV_KEY: keys}, name="MediaDeck Keyboard")
            self._pointer = UInput(
                {
                    ecodes.EV_KEY: [getattr(ecodes, code) for code in BUTTONS.values()],
                    ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
                },
                name="MediaDeck Pointer",
            )
        except (OSError, PermissionError) as exc:
            # Cas typique : règle udev uaccess non installée
            self.reason = f"cannot open /dev/uinput: {exc} (run 'make uinput')"
            self.close()
            log.warning(self.reason)

    @property
    def available(self) -> bool:
        return self._keyboard is not None and self._pointer is not None

    def close(self) -> None:
        self.release_all()  # jamais de bouton resté enfoncé après l'arrêt du serveur
        for device in (self._keyboard, self._pointer):
            if device is not None:
                device.close()
        self._keyboard = None
        self._pointer = None

    def snapshot(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason, "keys": sorted(KEY_NAMES), "buttons": sorted(self._held)}

    def release_all(self) -> None:
        self._release(list(self._held))

    def release_owned_by(self, owner: object) -> bool:
        """Relâche les boutons enfoncés par une connexion qui disparaît ; True si quelque chose a été relâché."""
        mine = [button for button, holder in self._held.items() if holder is owner]
        self._release(mine)
        return bool(mine)

    def _release(self, buttons: list[str]) -> None:
        for button in buttons:
            self._held.pop(button, None)
            if self._pointer is None or ecodes is None:
                continue
            try:
                self._pointer.write(ecodes.EV_KEY, getattr(ecodes, BUTTONS[button]), 0)
            except OSError as exc:  # l'état est déjà oublié : on relâche les suivants quand même
                log.warning("cannot release the %s button: %s", button, exc)
        if buttons and self._pointer is not None:
            try:
                self._pointer.syn()
            except OSError as exc:
                log.warning("cannot synchronise the pointer: %s", exc)

    def write_keys(self, events: list[tuple[int, bool]]) -> None:
        """Écrit des événements (code evdev, appuyée) puis synchronise ; réservé à la saisie de texte."""
        if not self.available or ecodes is None:
            raise RuntimeError(f"virtual input unavailable: {self.reason}")
        for code, pressed in events:
            if code not in TYPING_KEYS:
                raise ValueError(f"key code {code} not allowed")
            self._keyboard.write(ecodes.EV_KEY, code, 1 if pressed else 0)
        self._keyboard.syn()

    def execute(self, action: str, payload: dict[str, Any], owner: object | None = None) -> None:
        """Exécute une action du client. ``owner`` : la connexion, pour relâcher ses boutons si elle disparaît."""
        if not self.available or ecodes is None:
            raise RuntimeError(f"virtual input unavailable: {self.reason}")
        match action:
            case "key":
                name = payload.get("key")
                if name not in KEY_NAMES:
                    raise ValueError("unknown key")
                code = getattr(ecodes, KEY_NAMES[name])
                self._keyboard.write(ecodes.EV_KEY, code, 1)
                self._keyboard.write(ecodes.EV_KEY, code, 0)
                self._keyboard.syn()
            case "move":
                dx = _clamp_int(payload.get("dx"), MAX_MOVE)
                dy = _clamp_int(payload.get("dy"), MAX_MOVE)
                if dx:
                    self._pointer.write(ecodes.EV_REL, ecodes.REL_X, dx)
                if dy:
                    self._pointer.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                self._pointer.syn()
            case "scroll":
                dy = _clamp_int(payload.get("dy"), MAX_SCROLL)
                dx = _clamp_int(payload.get("dx", 0), MAX_SCROLL)
                if dy:
                    self._pointer.write(ecodes.EV_REL, ecodes.REL_WHEEL, dy)
                if dx:
                    self._pointer.write(ecodes.EV_REL, ecodes.REL_HWHEEL, dx)
                self._pointer.syn()
            case "button":
                # Bouton maintenu (bascule) : glisser-déposer, sélection ; relâché à la demande ou à la déconnexion
                button = payload.get("button")
                pressed = payload.get("pressed")
                if not isinstance(button, str) or button not in BUTTONS:
                    raise ValueError("unknown button")
                if not isinstance(pressed, bool):
                    raise ValueError("pressed must be a boolean")
                if pressed == (button in self._held):
                    return  # déjà dans cet état
                self._pointer.write(ecodes.EV_KEY, getattr(ecodes, BUTTONS[button]), 1 if pressed else 0)
                self._pointer.syn()
                if pressed:
                    self._held[button] = owner
                else:
                    del self._held[button]
            case "click":
                button = payload.get("button", "left")
                if button not in BUTTONS:
                    raise ValueError("unknown button")
                if button in self._held:
                    return  # un tapotement pendant un glissement ne doit pas lâcher le bouton
                code = getattr(ecodes, BUTTONS[button])
                self._pointer.write(ecodes.EV_KEY, code, 1)
                self._pointer.syn()
                self._pointer.write(ecodes.EV_KEY, code, 0)
                self._pointer.syn()
            case _:
                raise ValueError(f"unknown input action '{action}'")


def _clamp_int(value: Any, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric value expected")
    return max(-limit, min(limit, int(value)))

"""Simulateur de clavier pour les tests : rejoue des frappes dans un état xkb avec composition des touches mortes.

Reproduit ce que fait le serveur graphique (X11 ou Wayland) avec les touches reçues d'un périphérique uinput.
"""

import ctypes

from mediadeck.keymap import Keymap, Stroke

_COMPOSING, _COMPOSED, _CANCELLED = 1, 2, 3
_KEY_UP, _KEY_DOWN = 0, 1


class SimulatorUnavailable(Exception):
    """Table de composition introuvable pour la locale demandée."""


class Simulator:
    def __init__(self, keymap: Keymap, locale: str = "en_US.UTF-8") -> None:
        self.keymap = keymap
        lib = keymap.lib
        vp, u32, cp, sz = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t
        for name, restype, argtypes in (
            ("xkb_state_new", vp, [vp]),
            ("xkb_state_unref", None, [vp]),
            ("xkb_state_update_key", ctypes.c_int, [vp, u32, ctypes.c_int]),
            ("xkb_state_key_get_one_sym", u32, [vp, u32]),
            ("xkb_state_key_get_utf8", ctypes.c_int, [vp, u32, ctypes.c_char_p, sz]),
            ("xkb_compose_table_new_from_locale", vp, [vp, cp, ctypes.c_int]),
            ("xkb_compose_state_new", vp, [vp, ctypes.c_int]),
            ("xkb_compose_state_feed", ctypes.c_int, [vp, u32]),
            ("xkb_compose_state_get_status", ctypes.c_int, [vp]),
            ("xkb_compose_state_get_utf8", ctypes.c_int, [vp, ctypes.c_char_p, sz]),
            ("xkb_compose_state_reset", None, [vp]),
        ):
            function = getattr(lib, name)
            function.restype, function.argtypes = restype, argtypes
        self.lib = lib
        self.state = lib.xkb_state_new(keymap.handle)
        table = lib.xkb_compose_table_new_from_locale(keymap._context, locale.encode(), 0)
        if not table:
            raise SimulatorUnavailable(locale)
        self.compose = lib.xkb_compose_state_new(table, 0)

    def _utf8(self, function, *args) -> str:
        buffer = ctypes.create_string_buffer(64)
        function(*args, buffer, 64)
        return buffer.value.decode()

    def type(self, strokes: list[Stroke]) -> str:
        """Texte obtenu après ces frappes (les touches mortes en attente ne produisent rien)."""
        lib, output = self.lib, []
        for stroke in strokes:
            modifiers = [
                code
                for code, wanted in ((self.keymap.shift_code, stroke.shift), (self.keymap.altgr_code, stroke.altgr))
                if wanted and code is not None
            ]
            for code in modifiers:
                lib.xkb_state_update_key(self.state, code + 8, _KEY_DOWN)
            key = stroke.code + 8
            keysym = lib.xkb_state_key_get_one_sym(self.state, key)
            text = self._utf8(lib.xkb_state_key_get_utf8, self.state, key)
            lib.xkb_state_update_key(self.state, key, _KEY_DOWN)
            lib.xkb_state_update_key(self.state, key, _KEY_UP)
            for code in reversed(modifiers):
                lib.xkb_state_update_key(self.state, code + 8, _KEY_UP)
            lib.xkb_compose_state_feed(self.compose, keysym)
            status = lib.xkb_compose_state_get_status(self.compose)
            if status == _COMPOSING:
                continue
            if status == _COMPOSED:
                output.append(self._utf8(lib.xkb_compose_state_get_utf8, self.compose))
            elif status != _CANCELLED:
                output.append(text)
            lib.xkb_compose_state_reset(self.compose)
        return "".join(output)

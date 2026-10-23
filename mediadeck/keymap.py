"""Traduction caractère -> touches selon la disposition réelle du clavier du PC.

La disposition (AZERTY, QWERTY…) est lue dans la configuration de KDE (kxkbrc) ou via setxkbmap, puis compilée
avec libxkbcommon (ctypes, aucune dépendance Python). Chaque caractère devient la ou les touches que produirait
un vrai clavier : Maj, AltGr et touches mortes (é, ê, ñ…) comprises. Rien ne passe par le presse-papiers.
"""

from __future__ import annotations

import asyncio
import configparser
import ctypes
import ctypes.util
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .input import TYPING_KEYS

# Codes evdev (linux/input-event-codes.h)
KEY_ENTER = 28
KEY_TAB = 15
KEY_SPACE = 57
KEY_RIGHTALT = 100

# Keysyms X11 utiles (X11/keysymdef.h)
_LOG_LEVEL_CRITICAL = 10
_KEYSYM_SHIFT_L = 0xFFE1
_KEYSYM_ISO_LEVEL3_SHIFT = 0xFE03

# Marque combinante Unicode -> touche morte correspondante
_DEAD_KEYSYMS = {
    "̀": 0xFE50,  # grave
    "́": 0xFE51,  # aigu
    "̂": 0xFE52,  # circonflexe
    "̃": 0xFE53,  # tilde
    "̄": 0xFE54,  # macron
    "̆": 0xFE55,  # brève
    "̇": 0xFE56,  # point en chef
    "̈": 0xFE57,  # tréma
    "̊": 0xFE58,  # rond en chef
    "̋": 0xFE59,  # double aigu
    "̌": 0xFE5A,  # caron
    "̧": 0xFE5B,  # cédille
    "̨": 0xFE5C,  # ogonek
}

# Accents isolés : touche morte suivie d'une espace (« ^ » sur un clavier AZERTY, par exemple)
_SPACING_ACCENTS = {
    "^": 0xFE52,
    "`": 0xFE50,
    "~": 0xFE53,
    "¨": 0xFE57,
    "´": 0xFE51,
    "¸": 0xFE5B,
}

# Typographie des claviers tactiles, remplacée si la disposition du PC n'a pas le caractère
_TYPOGRAPHIC = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "«": '"',
    "»": '"',
    "…": "...",
    "–": "-",
    "—": "-",
    " ": " ",
    " ": " ",
    " ": " ",
}


class KeymapError(Exception):
    """Disposition du clavier introuvable ou bibliothèque xkbcommon absente."""


@dataclass(frozen=True)
class LayoutSpec:
    """Noms XKB d'une disposition (ceux de setxkbmap / kxkbrc)."""

    model: str = "pc105"
    layout: str = "us"
    variant: str = ""
    options: str = ""


@dataclass(frozen=True)
class Stroke:
    """Une frappe : une touche (code evdev), éventuellement avec Maj et/ou AltGr."""

    code: int
    shift: bool = False
    altgr: bool = False


# ----------------------------------------------------------------------
# libxkbcommon
# ----------------------------------------------------------------------


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


_library: ctypes.CDLL | None = None


def load_xkb() -> ctypes.CDLL:
    """Charge libxkbcommon une seule fois et déclare les prototypes utilisés."""
    global _library
    if _library is not None:
        return _library
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        raise KeymapError("libxkbcommon not found (apt install libxkbcommon0)") from exc
    u32, vp, cp, sz = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t
    prototypes: dict[str, tuple[Any, list[Any]]] = {
        "xkb_context_new": (vp, [ctypes.c_int]),
        "xkb_context_unref": (None, [vp]),
        "xkb_context_set_log_level": (None, [vp, ctypes.c_int]),
        "xkb_keymap_new_from_names": (vp, [vp, ctypes.POINTER(_RuleNames), ctypes.c_int]),
        "xkb_keymap_unref": (None, [vp]),
        "xkb_keymap_min_keycode": (u32, [vp]),
        "xkb_keymap_max_keycode": (u32, [vp]),
        "xkb_keymap_num_levels_for_key": (u32, [vp, u32, u32]),
        "xkb_keymap_key_get_syms_by_level": (
            ctypes.c_int,
            [vp, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))],
        ),
        "xkb_keymap_key_get_mods_for_level": (sz, [vp, u32, u32, u32, ctypes.POINTER(u32), sz]),
        "xkb_keymap_mod_get_index": (u32, [vp, cp]),
        "xkb_utf32_to_keysym": (u32, [u32]),
    }
    try:
        for function, (restype, argtypes) in prototypes.items():
            native = getattr(lib, function)
            native.restype = restype
            native.argtypes = argtypes
    except AttributeError as exc:
        raise KeymapError(f"libxkbcommon too old: {exc}") from exc
    _library = lib
    return lib


class Keymap:
    """Table keysym -> frappe pour une disposition donnée (premier groupe de la carte compilée)."""

    def __init__(self, spec: LayoutSpec) -> None:
        self.spec = spec
        self.lib = load_xkb()
        self._context: Any = self.lib.xkb_context_new(0)
        if not self._context:
            raise KeymapError("cannot create an xkb context")
        # Les erreurs de compilation deviennent une KeymapError : inutile de les écrire aussi sur stderr
        self.lib.xkb_context_set_log_level(self._context, _LOG_LEVEL_CRITICAL)
        names = _RuleNames(
            b"evdev", spec.model.encode(), spec.layout.encode(), spec.variant.encode(), spec.options.encode()
        )
        self.handle: Any = self.lib.xkb_keymap_new_from_names(self._context, ctypes.byref(names), 0)
        if not self.handle:
            self.lib.xkb_context_unref(self._context)
            self._context = None
            raise KeymapError(f"cannot compile the keyboard layout '{spec.layout}' variant '{spec.variant}'")
        self._table: dict[int, Stroke] = {}
        self.shift_code: int | None = None
        self.altgr_code: int | None = None
        self._build()

    def close(self) -> None:
        if self.handle:
            self.lib.xkb_keymap_unref(self.handle)
            self.handle = None
        if self._context:
            self.lib.xkb_context_unref(self._context)
            self._context = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - arrêt de l'interpréteur
            pass

    def _mod_bit(self, name: str) -> int:
        index = self.lib.xkb_keymap_mod_get_index(self.handle, name.encode())
        return 0 if index == 0xFFFFFFFF else 1 << index

    def _build(self) -> None:
        lib, handle = self.lib, self.handle
        shift_bit, level3_bit = self._mod_bit("Shift"), self._mod_bit("Mod5")
        candidates: list[tuple[int, Stroke]] = []
        for key in range(lib.xkb_keymap_min_keycode(handle), lib.xkb_keymap_max_keycode(handle) + 1):
            code = key - 8  # keycodes xkb = codes evdev + 8
            if code not in TYPING_KEYS:
                continue
            for level in range(lib.xkb_keymap_num_levels_for_key(handle, key, 0)):
                syms = ctypes.POINTER(ctypes.c_uint32)()
                if lib.xkb_keymap_key_get_syms_by_level(handle, key, 0, level, ctypes.byref(syms)) != 1:
                    continue
                keysym = int(syms[0])
                if level == 0 and keysym == _KEYSYM_SHIFT_L and self.shift_code is None:
                    self.shift_code = code
                elif level == 0 and keysym == _KEYSYM_ISO_LEVEL3_SHIFT and self.altgr_code is None:
                    self.altgr_code = code
                masks = (ctypes.c_uint32 * 8)()
                count = lib.xkb_keymap_key_get_mods_for_level(handle, key, 0, level, masks, 8)
                for mask in masks[:count]:
                    # Seuls Maj et AltGr sont simulables ; Ctrl, Alt et Super changeraient le sens de la frappe
                    if mask & ~(shift_bit | level3_bit):
                        continue
                    candidates.append((keysym, Stroke(code, bool(mask & shift_bit), bool(mask & level3_bit))))

        # Pour un même keysym, la frappe la plus simple : moins de modificateurs, puis code le plus petit
        best: dict[int, Stroke] = {}
        for keysym, stroke in candidates:
            if (stroke.shift and self.shift_code is None) or (stroke.altgr and self.altgr_code is None):
                continue
            current = best.get(keysym)
            if current is None or _cost(stroke) < _cost(current):
                best[keysym] = stroke
        self._table = best

    # ------------------------------------------------------------------
    # Texte -> frappes
    # ------------------------------------------------------------------

    def _direct(self, char: str) -> Stroke | None:
        if ord(char) < 0x20:
            return None
        return self._table.get(int(self.lib.xkb_utf32_to_keysym(ord(char))))

    def _sequence(self, char: str) -> list[Stroke] | None:
        if char == "\n":
            return [Stroke(KEY_ENTER)]
        if char == "\t":
            return [Stroke(KEY_TAB)]
        if char == "\r":
            return []
        direct = self._direct(char)
        if direct is not None:
            return [direct]
        replacement = _TYPOGRAPHIC.get(char)
        if replacement is not None:
            parts = [self._sequence(item) for item in replacement]
            if any(part is None for part in parts):
                return None
            return [stroke for part in parts if part is not None for stroke in part]
        # Lettre accentuée absente de la disposition : touche morte puis lettre de base (ê = ^ + e)
        decomposed = unicodedata.normalize("NFD", char)
        if len(decomposed) == 2 and decomposed[1] in _DEAD_KEYSYMS:
            dead = self._table.get(_DEAD_KEYSYMS[decomposed[1]])
            base = self._direct(decomposed[0])
            if dead is not None and base is not None:
                return [dead, base]
        accent = _SPACING_ACCENTS.get(char)
        if accent is not None:
            dead, space = self._table.get(accent), self._direct(" ")
            if dead is not None and space is not None:
                return [dead, space]
        return None

    def plan(self, text: str) -> tuple[list[Stroke], list[str]]:
        """Frappes à produire pour ce texte, et caractères impossibles à saisir avec cette disposition."""
        strokes: list[Stroke] = []
        skipped: list[str] = []
        for char in text:
            sequence = self._sequence(char)
            if sequence is None:
                if char not in skipped:
                    skipped.append(char)
            else:
                strokes.extend(sequence)
        return strokes, skipped


def _cost(stroke: Stroke) -> tuple[int, int]:
    return int(stroke.shift) + int(stroke.altgr), stroke.code


# ----------------------------------------------------------------------
# Détection de la disposition courante
# ----------------------------------------------------------------------


class _CaseSensitiveParser(configparser.RawConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _specs(model: str, layouts: str, variants: str, options: str) -> list[LayoutSpec]:
    names = [name.strip() for name in layouts.split(",") if name.strip()]
    variant_list = [variant.strip() for variant in variants.split(",")]
    variant_list += [""] * (len(names) - len(variant_list))
    return [
        LayoutSpec(model.strip() or "pc105", name, variant_list[index], options.strip())
        for index, name in enumerate(names)
    ]


def parse_kxkbrc(text: str) -> list[LayoutSpec] | None:
    """Dispositions déclarées dans le fichier kxkbrc de KDE, ou None s'il ne s'applique pas."""
    parser = _CaseSensitiveParser(strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("Layout") or parser.get("Layout", "Use", fallback="false").lower() != "true":
        return None
    specs = _specs(
        parser.get("Layout", "Model", fallback=""),
        parser.get("Layout", "LayoutList", fallback=""),
        parser.get("Layout", "VariantList", fallback=""),
        parser.get("Layout", "Options", fallback=""),
    )
    return specs or None


def parse_setxkbmap(text: str) -> list[LayoutSpec] | None:
    """Dispositions de la sortie de ``setxkbmap -query`` (session X11)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()
    specs = _specs(
        values.get("model", ""), values.get("layout", ""), values.get("variant", ""), values.get("options", "")
    )
    return specs or None


async def _configured_layouts() -> list[LayoutSpec]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    try:
        specs = parse_kxkbrc(await asyncio.to_thread((config_home / "kxkbrc").read_text, "utf-8"))
    except OSError:
        specs = None
    if specs:
        return specs
    try:
        process = await asyncio.create_subprocess_exec(
            "setxkbmap", "-query", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        output, _ = await process.communicate()
        specs = parse_setxkbmap(output.decode(errors="replace"))
    except OSError:
        specs = None
    if specs:
        return specs
    raise KeymapError("cannot detect the keyboard layout (no kxkbrc, no setxkbmap)")


async def _kde_layout_index(bus: Any) -> int:
    """Index de la disposition active selon KDE (0 si le service est absent)."""
    try:
        introspection = await bus.introspect("org.kde.keyboard", "/Layouts")
        proxy = bus.get_proxy_object("org.kde.keyboard", "/Layouts", introspection)
        layouts: Any = proxy.get_interface("org.kde.KeyboardLayouts")
        return int(await layouts.call_get_layout())
    except Exception:  # noqa: BLE001 - pas de KDE ou bus indisponible
        return 0


async def current_layout(bus: Any | None) -> LayoutSpec:
    """Disposition réellement active : la liste configurée, l'index choisi par KDE si plusieurs sont déclarées."""
    specs = await _configured_layouts()
    index = await _kde_layout_index(bus) if bus is not None and len(specs) > 1 else 0
    return specs[index] if 0 <= index < len(specs) else specs[0]

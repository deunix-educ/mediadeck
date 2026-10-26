"""Tests de la saisie de texte : disposition du clavier, traduction caractère -> touches, envoi des frappes."""

import asyncio
import unittest
from typing import Any

from mediadeck.input import TYPING_KEYS, VirtualInput
from mediadeck.keymap import (
    KEY_ENTER,
    KEY_SPACE,
    Keymap,
    KeymapError,
    LayoutSpec,
    Stroke,
    parse_kxkbrc,
    parse_setxkbmap,
)
from mediadeck.text import MAX_TEXT_LENGTH, TextTyper

from xkbsim import Simulator, SimulatorUnavailable

US = LayoutSpec(layout="us")
FR = LayoutSpec(layout="fr")
FR_LATIN9 = LayoutSpec(model="pc104", layout="fr", variant="latin9")
DE = LayoutSpec(layout="de")

KEY_A, KEY_Q, KEY_2, KEY_0 = 30, 16, 3, 11
KEY_LEFTSHIFT, KEY_RIGHTALT = 42, 100

ASCII = "Hello, World! 0123456789 @#$%^&*()_+-=[]{};:'\",.<>/?\\|`~ end"
FRENCH = "Élève à l'école, ça coûte 5 €, garçon naïf, forêt, Noël, Ça va ? Œuvre : « oui »."


def build(spec: LayoutSpec) -> Keymap:
    try:
        return Keymap(spec)
    except KeymapError as exc:
        raise unittest.SkipTest(f"xkb unavailable for {spec}: {exc}") from exc


class ParseTest(unittest.TestCase):
    def test_kxkbrc(self) -> None:
        text = "[Layout]\nDisplayNames=,\nLayoutList=fr,us\nModel=pc104\nOptions=compose:ralt\nUse=true\nVariantList=latin9,\n"
        self.assertEqual(
            parse_kxkbrc(text),
            [
                LayoutSpec("pc104", "fr", "latin9", "compose:ralt"),
                LayoutSpec("pc104", "us", "", "compose:ralt"),
            ],
        )

    def test_kxkbrc_not_applicable(self) -> None:
        self.assertIsNone(parse_kxkbrc("[Layout]\nLayoutList=fr\nUse=false\n"))
        self.assertIsNone(parse_kxkbrc("[Other]\nUse=true\n"))
        self.assertIsNone(parse_kxkbrc("[Layout]\nUse=true\nLayoutList=\n"))
        self.assertIsNone(parse_kxkbrc("pas un fichier ini"))

    def test_kxkbrc_defaults_model(self) -> None:
        specs = parse_kxkbrc("[Layout]\nUse=true\nLayoutList=de\n")
        assert specs is not None
        self.assertEqual(specs[0], LayoutSpec("pc105", "de", "", ""))

    def test_setxkbmap(self) -> None:
        output = "rules:      evdev\nmodel:      pc104\nlayout:     fr,us\nvariant:    latin9,\noptions:    grp:alt_shift_toggle\n"
        self.assertEqual(
            parse_setxkbmap(output),
            [
                LayoutSpec("pc104", "fr", "latin9", "grp:alt_shift_toggle"),
                LayoutSpec("pc104", "us", "", "grp:alt_shift_toggle"),
            ],
        )
        self.assertIsNone(parse_setxkbmap(""))


class KeymapTest(unittest.TestCase):
    def test_us_basic(self) -> None:
        keymap = build(US)
        self.assertEqual(keymap.plan("a")[0], [Stroke(KEY_A)])
        self.assertEqual(keymap.plan("A")[0], [Stroke(KEY_A, shift=True)])
        self.assertEqual(keymap.plan("@")[0], [Stroke(KEY_2, shift=True)])
        self.assertEqual(keymap.plan(" ")[0], [Stroke(KEY_SPACE)])
        self.assertEqual(keymap.plan("\n")[0], [Stroke(KEY_ENTER)])
        self.assertEqual((keymap.shift_code, keymap.altgr_code), (KEY_LEFTSHIFT, None))

    def test_azerty_differs_from_qwerty(self) -> None:
        # Le piège d'origine : la même lettre ne se tape pas avec la même touche
        keymap = build(FR)
        self.assertEqual(keymap.plan("a")[0], [Stroke(KEY_Q)])
        self.assertEqual(keymap.plan("1")[0], [Stroke(2, shift=True)])
        self.assertEqual(keymap.plan("é")[0], [Stroke(KEY_2)])
        self.assertEqual(keymap.plan("@")[0], [Stroke(KEY_0, altgr=True)])
        self.assertEqual(keymap.altgr_code, KEY_RIGHTALT)

    def test_unavailable_characters_are_reported_once(self) -> None:
        strokes, skipped = build(US).plan("a→b→😀")
        self.assertEqual(len(strokes), 2)
        self.assertEqual(skipped, ["→", "😀"])

    def test_typographic_fallback(self) -> None:
        keymap = build(US)
        apostrophe = keymap.plan("'")[0]
        self.assertEqual(keymap.plan("’")[0], apostrophe)
        self.assertEqual(keymap.plan("…")[0], keymap.plan("...")[0])

    def test_only_allowed_keys_are_used(self) -> None:
        keymap = build(FR_LATIN9)
        strokes, _ = keymap.plan(ASCII + FRENCH)
        self.assertTrue(all(stroke.code in TYPING_KEYS for stroke in strokes))

    def test_invalid_layout(self) -> None:
        with self.assertRaises(KeymapError):
            Keymap(LayoutSpec(layout="zz-not-a-layout"))


class RoundTripTest(unittest.TestCase):
    """Les frappes calculées, rejouées par un état xkb, redonnent exactement le texte demandé."""

    def simulate(self, spec: LayoutSpec, text: str) -> tuple[str, list[str]]:
        keymap = build(spec)
        try:
            simulator = Simulator(keymap)
        except SimulatorUnavailable as exc:
            raise unittest.SkipTest(f"no compose table for {exc}") from exc
        strokes, skipped = keymap.plan(text)
        return simulator.type(strokes), skipped

    def test_ascii_on_us(self) -> None:
        typed, skipped = self.simulate(US, ASCII)
        self.assertEqual((typed, skipped), (ASCII, []))

    def test_ascii_on_azerty_latin9(self) -> None:
        typed, skipped = self.simulate(FR_LATIN9, ASCII)
        self.assertEqual((typed, skipped), (ASCII, []))

    def test_ascii_on_german(self) -> None:
        typed, skipped = self.simulate(DE, ASCII)
        self.assertEqual((typed, skipped), (ASCII, []))

    def test_french_text_on_azerty(self) -> None:
        typed, skipped = self.simulate(FR_LATIN9, FRENCH)
        self.assertEqual(skipped, [])
        self.assertEqual(typed, FRENCH)

    def test_accents_with_dead_keys(self) -> None:
        # ê, ï, ñ, ü, â : absents de la rangée de touches ou obtenus par touche morte
        text = "ê ï ñ ü â Ê Ö ã û"
        typed, skipped = self.simulate(FR_LATIN9, text)
        self.assertEqual((typed, skipped), (text, []))

    def test_partial_text_only_loses_the_unavailable_characters(self) -> None:
        typed, skipped = self.simulate(US, "abc → def")
        self.assertEqual(skipped, ["→"])
        self.assertEqual(typed, "abc  def")

    def test_newline_and_tab(self) -> None:
        typed, _ = self.simulate(US, "a\tb\nc")
        # Entrée produit « \r » dans xkb ; les applications le lisent comme la touche Entrée
        self.assertEqual(typed, "a\tb\rc")


class FakeKeyboard:
    """Remplace VirtualInput : enregistre les événements et suit les touches enfoncées."""

    available = True
    reason = ""

    def __init__(self) -> None:
        self.events: list[tuple[int, bool]] = []
        self.down: set[int] = set()

    def write_keys(self, events: list[tuple[int, bool]]) -> None:
        for code, pressed in events:
            assert code in TYPING_KEYS
            self.events.append((code, pressed))
            (self.down.add if pressed else self.down.discard)(code)


def typer(keyboard: Any = None, enabled: bool = True, spec: LayoutSpec = US, delay: float = 0) -> TextTyper:
    async def layout() -> LayoutSpec:
        return spec

    return TextTyper(keyboard or FakeKeyboard(), enabled, layout, delay)


class TextTyperTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_order_with_modifiers(self) -> None:
        keyboard = FakeKeyboard()
        await typer(keyboard).type("aA")
        self.assertEqual(
            keyboard.events,
            [
                (KEY_A, True), (KEY_A, False),
                (KEY_LEFTSHIFT, True), (KEY_A, True), (KEY_A, False), (KEY_LEFTSHIFT, False),
            ],
        )
        self.assertEqual(keyboard.down, set())

    async def test_layout_is_read_on_each_call(self) -> None:
        keyboard = FakeKeyboard()
        spec = [US]

        async def layout() -> LayoutSpec:
            return spec[0]

        text_typer = TextTyper(keyboard, True, layout, 0)
        await text_typer.type("a")
        spec[0] = FR
        await text_typer.type("a")
        self.assertEqual([code for code, pressed in keyboard.events if pressed], [KEY_A, KEY_Q])

    async def test_unavailable_characters_are_typed_around_then_reported(self) -> None:
        keyboard = FakeKeyboard()
        with self.assertRaisesRegex(ValueError, "→"):
            await typer(keyboard).type("a→b")
        self.assertEqual([code for code, pressed in keyboard.events if pressed], [KEY_A, 48])

    async def test_validation(self) -> None:
        for bad in ("", None, 5, ["a"], "x" * (MAX_TEXT_LENGTH + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await typer().type(bad)

    async def test_disabled_by_config(self) -> None:
        disabled = typer(enabled=False)
        self.assertFalse(disabled.available)
        self.assertIn("features.text_input", disabled.snapshot()["reason"])
        with self.assertRaises(RuntimeError):
            await disabled.type("a")

    async def test_keyboard_unavailable(self) -> None:
        keyboard = FakeKeyboard()
        keyboard.available = False
        keyboard.reason = "cannot open /dev/uinput"
        text_typer = typer(keyboard)
        self.assertEqual(text_typer.snapshot(), {"available": False, "reason": "cannot open /dev/uinput"})

    async def test_concurrent_texts_do_not_interleave(self) -> None:
        keyboard = FakeKeyboard()
        text_typer = typer(keyboard, delay=0.001)
        await asyncio.gather(text_typer.type("aaaa"), text_typer.type("bbbb"))
        pressed = [code for code, is_down in keyboard.events if is_down]
        self.assertEqual(pressed, [KEY_A] * 4 + [48] * 4)

    async def test_cancellation_never_leaves_a_key_down(self) -> None:
        keyboard = FakeKeyboard()
        text_typer = typer(keyboard, delay=0.05)
        task = asyncio.create_task(text_typer.type("AAAA"))
        await asyncio.sleep(0.02)  # Maj + A sont enfoncées, la pause est en cours
        self.assertTrue(keyboard.down)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(keyboard.down, set())


class TextTyperStartupLogTest(unittest.TestCase):
    """L'état de la saisie est journalisé au démarrage : sans cela, un bouton grisé n'a aucune trace côté PC."""

    def test_disabled_in_config_is_logged_with_the_setting_name(self) -> None:
        with self.assertLogs("mediadeck.text", level="INFO") as logs:
            typer(enabled=False)
        self.assertIn("features.text_input", "\n".join(logs.output))

    def test_enabled_is_logged(self) -> None:
        with self.assertLogs("mediadeck.text", level="INFO") as logs:
            typer(enabled=True)
        self.assertIn("text entry enabled", "\n".join(logs.output))


class VirtualInputKeysTest(unittest.TestCase):
    class Device:
        def __init__(self) -> None:
            self.written: list[tuple[int, int, int]] = []
            self.synced = 0

        def write(self, event_type: int, code: int, value: int) -> None:
            self.written.append((event_type, code, value))

        def syn(self) -> None:
            self.synced += 1

        def close(self) -> None:
            pass

    def make(self) -> tuple[VirtualInput, "VirtualInputKeysTest.Device"]:
        keyboard_input = VirtualInput(False)
        device = self.Device()
        keyboard_input._keyboard = device
        keyboard_input._pointer = self.Device()
        return keyboard_input, device

    def test_write_keys_syncs_once(self) -> None:
        keyboard_input, device = self.make()
        keyboard_input.write_keys([(KEY_LEFTSHIFT, True), (KEY_A, True)])
        self.assertEqual([(code, value) for _type, code, value in device.written], [(KEY_LEFTSHIFT, 1), (KEY_A, 1)])
        self.assertEqual(device.synced, 1)

    def test_keys_outside_the_typing_block_are_refused(self) -> None:
        keyboard_input, device = self.make()
        for code in (116, 142, 205, 0, 300):  # POWER, SLEEP, SUSPEND, hors plage
            with self.subTest(code=code), self.assertRaises(ValueError):
                keyboard_input.write_keys([(code, True)])
        self.assertEqual(device.written, [])

    def test_unavailable_input(self) -> None:
        with self.assertRaises(RuntimeError):
            VirtualInput(False).write_keys([(KEY_A, True)])


if __name__ == "__main__":
    unittest.main()

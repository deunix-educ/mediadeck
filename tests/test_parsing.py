"""Tests des conversions MPRIS et pactl, sans D-Bus ni serveur audio."""

import json
import unittest

from dbus_next.signature import Variant

from mediadeck.audio import parse_sinks
from mediadeck.mpris import normalize_metadata, unwrap


class MprisTest(unittest.TestCase):
    def test_unwrap_nested_variants(self) -> None:
        value = {"a": Variant("as", ["x", "y"]), "b": Variant("v", Variant("i", 3))}
        self.assertEqual(unwrap(value), {"a": ["x", "y"], "b": 3})

    def test_normalize_metadata(self) -> None:
        raw = {
            "mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/Track/1"),
            "mpris:length": Variant("x", 215_000_000),
            "xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A", "B"]),
            "xesam:comment": Variant("as", ["ignored"]),
        }
        meta = normalize_metadata(raw)
        self.assertEqual(meta["artist"], "A, B")
        self.assertEqual(meta["length"], 215_000_000)
        self.assertEqual(meta["trackid"], "/org/mpris/MediaPlayer2/Track/1")
        self.assertNotIn("comment", meta)

    def test_missing_length_defaults_to_zero(self) -> None:
        self.assertEqual(normalize_metadata({})["length"], 0)


class PactlTest(unittest.TestCase):
    def test_parse_sinks_average_volume(self) -> None:
        raw = json.dumps(
            [
                {
                    "name": "alsa_output.hdmi",
                    "description": "HDMI TV",
                    "mute": False,
                    "volume": {
                        "front-left": {"value": 26214, "value_percent": "40%", "db": "-23.88 dB"},
                        "front-right": {"value": 32768, "value_percent": "50%", "db": "-18.06 dB"},
                    },
                },
                {"name": "bt", "description": "", "mute": True, "volume": {}},
            ]
        )
        sinks = parse_sinks(raw)
        self.assertEqual(sinks[0]["volume"], 0.45)
        self.assertFalse(sinks[0]["muted"])
        # Description vide : repli sur le nom
        self.assertEqual(sinks[1]["description"], "bt")
        self.assertTrue(sinks[1]["muted"])
        self.assertEqual(sinks[1]["volume"], 0.0)


if __name__ == "__main__":
    unittest.main()

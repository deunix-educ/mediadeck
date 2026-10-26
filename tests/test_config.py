"""Tests de validation de la configuration."""

import os
import tempfile
import unittest
from pathlib import Path

from mediadeck.config import ConfigError, load_config, parse_config

TOKEN = "x" * 32


class ParseConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = parse_config({"server": {"token": TOKEN}})
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.max_volume, 1.0)
        self.assertTrue(config.enable_input)
        self.assertEqual(config.launchers, ())

    def test_short_token_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"server": {"token": "short"}})

    def test_launcher_command_must_be_argv(self) -> None:
        # Une chaîne seule serait tentante à passer à un shell : refusée
        with self.assertRaises(ConfigError):
            parse_config({"server": {"token": TOKEN}, "launchers": [{"id": "a", "label": "A", "command": "vlc"}]})

    def test_duplicate_launcher_id(self) -> None:
        launcher = {"id": "vlc", "label": "VLC", "command": ["vlc"]}
        with self.assertRaises(ConfigError):
            parse_config({"server": {"token": TOKEN}, "launchers": [launcher, launcher]})

    def test_advertise_host(self) -> None:
        self.assertEqual(parse_config({"server": {"token": TOKEN}}).advertise_host, "")
        config = parse_config({"server": {"token": TOKEN, "advertise_host": "192.168.1.104"}})
        self.assertEqual(config.advertise_host, "192.168.1.104")
        self.assertEqual(
            parse_config({"server": {"token": TOKEN, "advertise_host": "mediapc.local"}}).advertise_host,
            "mediapc.local",
        )

    def test_advertise_host_rejects_url_syntax(self) -> None:
        # La valeur est insérée telle quelle dans l'URL : pas de schéma, port, chemin ni espace
        for value in ("http://192.168.1.104", "192.168.1.104:8765", "host/path", "a b", "x@y", 42):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config({"server": {"token": TOKEN, "advertise_host": value}})

    def test_tls_paths(self) -> None:
        config = parse_config({"server": {"token": TOKEN, "tls_cert": "~/c.pem", "tls_key": "/k.key"}})
        self.assertTrue(config.tls_enabled)
        self.assertEqual(config.tls_cert, Path.home() / "c.pem")
        self.assertIsNone(config.tls_ca)
        self.assertFalse(parse_config({"server": {"token": TOKEN}}).tls_enabled)

    def test_tls_options_are_consistent(self) -> None:
        for server in (
            {"tls_cert": "/c.pem"},  # clé manquante
            {"tls_key": "/k.key"},  # certificat manquant
            {"tls_ca": "/ca.crt"},  # CA sans certificat
            {"tls_cert": 1, "tls_key": "/k.key"},
        ):
            with self.subTest(server=server), self.assertRaises(ConfigError):
                parse_config({"server": {"token": TOKEN, **server}})

    def test_http_port_is_off_by_default(self) -> None:
        self.assertEqual(parse_config({"server": {"token": TOKEN}}).http_port, 0)

    def test_http_port_next_to_tls(self) -> None:
        server = {"token": TOKEN, "tls_cert": "/c.pem", "tls_key": "/k.key", "http_port": 8766}
        self.assertEqual(parse_config({"server": server}).http_port, 8766)

    def test_http_port_is_validated(self) -> None:
        tls = {"tls_cert": "/c.pem", "tls_key": "/k.key"}
        for server in (
            {"http_port": 8766},  # sans TLS, `port` sert déjà du HTTP en clair
            {**tls, "http_port": 8765},  # même port que le HTTPS
            {**tls, "http_port": 70000},
            {**tls, "http_port": -1},
            {**tls, "http_port": "8766"},
            {**tls, "http_port": True},
        ):
            with self.subTest(server=server), self.assertRaises(ConfigError):
                parse_config({"server": {"token": TOKEN, **server}})

    def test_max_volume_bounds(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"server": {"token": TOKEN}, "audio": {"max_volume": 3}})


class LoadConfigTest(unittest.TestCase):
    def test_world_readable_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'[server]\ntoken = "{TOKEN}"\n')
            os.chmod(path, 0o644)
            with self.assertRaises(ConfigError):
                load_config(path)
            os.chmod(path, 0o600)
            self.assertEqual(load_config(path).token, TOKEN)

    def test_example_config_is_valid(self) -> None:
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(example.read_text().replace("CHANGE_ME", TOKEN))
            os.chmod(path, 0o600)
            config = load_config(path)
            self.assertGreater(len(config.launchers), 0)


if __name__ == "__main__":
    unittest.main()

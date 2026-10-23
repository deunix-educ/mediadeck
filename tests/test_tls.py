"""Tests du HTTPS local : génération des certificats, validation et poignée de main TLS."""

import asyncio
import os
import shutil
import ssl
import tempfile
import unittest
from pathlib import Path

from mediadeck.__main__ import client_url
from mediadeck.config import Config, ConfigError, load_config, parse_config
from mediadeck.tls import build_ssl_context, enable_tls, generate_certificates, is_permitted

TOKEN = "x" * 32
LAN_IP = "192.168.1.104"


class PermittedNamesTest(unittest.TestCase):
    def test_private_addresses_and_local_names(self) -> None:
        for host in (LAN_IP, "10.2.0.2", "172.20.0.1", "127.0.0.1", "localhost", "pc.local", "pc.lan", "pc"):
            with self.subTest(host=host):
                self.assertTrue(is_permitted(host, "pc"))

    def test_public_names_refused(self) -> None:
        for host in ("8.8.8.8", "172.32.0.1", "example.com", "evil.local.example.com", "::1"):
            with self.subTest(host=host):
                self.assertFalse(is_permitted(host, "pc"))


class ClientUrlTest(unittest.TestCase):
    def test_scheme_follows_tls(self) -> None:
        server = {"token": TOKEN, "advertise_host": LAN_IP}
        self.assertTrue(client_url(parse_config({"server": server})).startswith(f"http://{LAN_IP}:8765/?token="))
        tls = {**server, "tls_cert": "/c.pem", "tls_key": "/k.key"}
        self.assertTrue(client_url(parse_config({"server": tls})).startswith(f"https://{LAN_IP}:8765/?token="))


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CertificateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls._tmp.name) / "tls"
        cls.ca, cls.cert, cls.key = generate_certificates(cls.directory, [LAN_IP, "localhost", "127.0.0.1"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _config(self) -> Config:
        server = {"token": TOKEN, "tls_cert": str(self.cert), "tls_key": str(self.key), "tls_ca": str(self.ca)}
        return parse_config({"server": server})

    def test_private_keys_are_personal(self) -> None:
        self.assertEqual((self.directory / "ca.key").stat().st_mode & 0o077, 0)
        self.assertEqual(self.key.stat().st_mode & 0o077, 0)
        self.assertEqual(self.directory.stat().st_mode & 0o077, 0)

    def test_public_name_refused(self) -> None:
        with self.assertRaises(ConfigError):
            generate_certificates(self.directory, ["example.com"])

    def test_reissue_keeps_the_ca(self) -> None:
        before = self.ca.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            # Copie du dossier : le certificat serveur est réémis, la CA (donc la confiance de la tablette) reste
            copy = Path(tmp) / "tls"
            shutil.copytree(self.directory, copy)
            generate_certificates(copy, [LAN_IP])
            self.assertEqual((copy / "mediadeck-ca.crt").read_bytes(), before)

    def test_world_readable_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "server.key"
            shutil.copy(self.key, key)
            os.chmod(key, 0o644)
            server = {"token": TOKEN, "tls_cert": str(self.cert), "tls_key": str(key)}
            with self.assertRaises(ConfigError):
                build_ssl_context(parse_config({"server": server}))

    def test_missing_file_rejected(self) -> None:
        server = {"token": TOKEN, "tls_cert": "/nonexistent.pem", "tls_key": str(self.key)}
        with self.assertRaises(ConfigError):
            build_ssl_context(parse_config({"server": server}))

    def test_no_tls_without_certificate(self) -> None:
        self.assertIsNone(build_ssl_context(parse_config({"server": {"token": TOKEN}})))

    def test_handshake_verifies_with_the_ca(self) -> None:
        # Le client se connecte en local mais vérifie le nom annoncé à la tablette
        for name in (LAN_IP, "localhost", "127.0.0.1"):
            with self.subTest(name=name):
                asyncio.run(self._handshake(name, cafile=self.ca))

    def test_handshake_refused_for_other_address(self) -> None:
        with self.assertRaises(ssl.SSLCertVerificationError):
            asyncio.run(self._handshake("192.168.1.105", cafile=self.ca))

    def test_handshake_refused_without_the_ca(self) -> None:
        with self.assertRaises(ssl.SSLCertVerificationError):
            asyncio.run(self._handshake(LAN_IP, cafile=None))

    async def _handshake(self, server_hostname: str, cafile: Path | None) -> None:
        context = build_ssl_context(self._config())
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        client = ssl.create_default_context(cafile=str(cafile) if cafile else None)
        try:
            _reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client, server_hostname=server_hostname
            )
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()


class EnableTlsTest(unittest.TestCase):
    def test_inserts_paths_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(f'[server]\nhost = "0.0.0.0"\nport = 8765\ntoken = "{TOKEN}"\n\n[audio]\nmax_volume = 1.0\n')
            os.chmod(path, 0o600)
            cert, key, ca = Path("/t/server.pem"), Path("/t/server.key"), Path("/t/ca.crt")
            self.assertTrue(enable_tls(path, cert, key, ca))
            self.assertFalse(enable_tls(path, cert, key, ca))
            config = load_config(path)
            self.assertEqual((config.tls_cert, config.tls_key, config.tls_ca), (cert, key, ca))
            self.assertEqual(config.max_volume, 1.0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_port_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(f'[server]\ntoken = "{TOKEN}"\n')
            with self.assertRaises(ConfigError):
                enable_tls(path, Path("/c"), Path("/k"), Path("/a"))


if __name__ == "__main__":
    unittest.main()

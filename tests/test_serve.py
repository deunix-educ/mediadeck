"""Tests des écoutes : HTTPS seul, ou HTTPS + HTTP en clair (server.http_port), avec une vraie CA locale."""

import dataclasses
import shutil
import socket
import ssl
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from mediadeck.config import Config, parse_config
from mediadeck.server import open_sites
from mediadeck.tls import build_ssl_context, generate_certificates

TOKEN = "x" * 32


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def hello(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class OpenSitesTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ca, cls.cert, cls.key = generate_certificates(Path(cls._tmp.name) / "tls", ["localhost", "127.0.0.1"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _config(self, plain: bool) -> Config:
        server = {
            "token": TOKEN,
            "host": "127.0.0.1",
            "port": free_port(),
            "tls_cert": str(self.cert),
            "tls_key": str(self.key),
            "tls_ca": str(self.ca),
        }
        config = parse_config({"server": server})
        return dataclasses.replace(config, http_port=free_port()) if plain else config

    async def _serve(self, config: Config) -> None:
        app = web.Application()
        app.router.add_get("/", hello)
        runner = await open_sites(app, config, build_ssl_context(config))
        self.addAsyncCleanup(runner.cleanup)

    async def _get(self, url: str) -> str:
        context = ssl.create_default_context(cafile=str(self.ca))
        async with aiohttp.ClientSession() as session, session.get(url, ssl=context) as response:
            self.assertEqual(response.status, 200)
            return await response.text()

    async def test_https_only_by_default(self) -> None:
        config = self._config(plain=False)
        await self._serve(config)
        self.assertEqual(await self._get(f"https://127.0.0.1:{config.port}/"), "ok")
        with self.assertRaises(aiohttp.ClientError):
            await self._get(f"http://127.0.0.1:{config.port}/")

    async def test_secure_and_unsecured_side_by_side(self) -> None:
        config = self._config(plain=True)
        await self._serve(config)
        self.assertEqual(await self._get(f"https://127.0.0.1:{config.port}/"), "ok")
        self.assertEqual(await self._get(f"http://127.0.0.1:{config.http_port}/"), "ok")

    async def test_each_port_keeps_its_protocol(self) -> None:
        config = self._config(plain=True)
        await self._serve(config)
        with self.assertRaises(aiohttp.ClientError):
            await self._get(f"http://127.0.0.1:{config.port}/")
        with self.assertRaises(aiohttp.ClientError):
            await self._get(f"https://127.0.0.1:{config.http_port}/")

    async def test_busy_http_port_releases_the_main_port(self) -> None:
        config = self._config(plain=True)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", config.http_port))
            busy.listen()
            with self.assertRaises(OSError):
                await open_sites(web.Application(), config, build_ssl_context(config))
        # Le port principal doit être libéré par l'échec : on peut le rouvrir aussitôt
        await self._serve(dataclasses.replace(config, http_port=0))

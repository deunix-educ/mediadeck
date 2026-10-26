"""Tests des en-têtes de sécurité et de cache posés par le serveur."""

import os
import re
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mediadeck.server import CSP, STATIC_DIR, WEB_DIR, render_page, security_headers, static_version, versioned_static


async def page(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def art(_request: web.Request) -> web.Response:
    return web.Response(text="x", headers={"Cache-Control": "private, max-age=3600"})


class HeadersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / "app.js").write_text("console.log(1)")
        app = web.Application(middlewares=[security_headers])
        app.router.add_static("/static", self._tmp.name)
        app.router.add_get("/page", page)
        app.router.add_get("/art", art)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self._tmp.cleanup()

    async def test_pages_and_static_files_are_revalidated(self) -> None:
        for path in ("/page", "/static/app.js"):
            response = await self.client.get(path)
            self.assertEqual(response.headers["Cache-Control"], "no-cache", path)
            self.assertEqual(response.headers["Content-Security-Policy"], CSP)

    async def test_conditional_request_gets_a_304(self) -> None:
        first = await self.client.get("/static/app.js")
        etag = first.headers["ETag"]
        again = await self.client.get("/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(again.status, 304)

    async def test_existing_cache_policy_is_kept(self) -> None:
        response = await self.client.get("/art")
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=3600")


class StaticVersionTest(unittest.TestCase):
    def test_version_follows_the_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "app.js").write_text("a")
            first = static_version(directory)
            self.assertEqual(static_version(directory), first)
            (directory / "app.js").write_text("b")  # même taille : la date suffit à changer l'empreinte
            os.utime(directory / "app.js", ns=(2_000_000_000, 2_000_000_000))
            second = static_version(directory)
            self.assertNotEqual(second, first)
            (directory / "style.css").write_text("c")
            self.assertNotEqual(static_version(directory), second)

    def test_pages_point_to_versioned_files(self) -> None:
        for name in ("index.html", "admin.html"):
            html = render_page(name).text or ""
            version = static_version()
            self.assertNotRegex(html, r'"/static/(?!%s/)' % version, name)
            self.assertIn(f'"/static/{version}/', html)
            self.assertEqual(len(re.findall(r'"/static/', html)), len(re.findall(rf'"/static/{version}/', html)), name)

    def test_html_source_files_only_reference_static_with_a_leading_slash_quote(self) -> None:
        # render_page remplace '"/static/' : toute autre forme de référence échapperait au versionnement
        for name in ("index.html", "admin.html"):
            source = (WEB_DIR / name).read_text(encoding="utf-8")
            self.assertEqual(source.count("/static/"), source.count('"/static/'), name)


class VersionedStaticTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = web.Application(middlewares=[security_headers])
        app.router.add_get("/static/{version}/{path:.+}", versioned_static)
        app.router.add_static("/static", STATIC_DIR, show_index=False, follow_symlinks=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_versioned_url_serves_the_current_file_with_an_immutable_cache(self) -> None:
        response = await self.client.get("/static/whatever/app.js")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertIn("class MediaPanel", await response.text())

    async def test_relative_imports_resolve_under_the_same_version(self) -> None:
        response = await self.client.get("/static/whatever/common.js")
        self.assertEqual(response.status, 200)
        self.assertIn("class I18n", await response.text())

    async def test_old_addresses_still_work_but_are_revalidated(self) -> None:
        response = await self.client.get("/static/app.js")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    async def test_nothing_outside_the_static_folder(self) -> None:
        for path in (
            "/static/v/..%2F..%2Fserver.py", "/static/v/%2e%2e/%2e%2e/server.py", "/static/v/../../server.py",
            "/static/v/%2Fetc%2Fpasswd", "/static/v/absent.js", "/static/v/",
        ):
            response = await self.client.get(path)
            self.assertIn(response.status, (400, 404), path)
            self.assertNotIn("import", (await response.text())[:200], path)


if __name__ == "__main__":
    unittest.main()

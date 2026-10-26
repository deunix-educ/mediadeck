"""Bouton « Clavier » grisé : toucher le bouton doit dire pourquoi (une infobulle est invisible sur une tablette).

Régression visée : sur une installation neuve `features.text_input` est faux, le bouton était seulement grisé et
la raison n'apparaissait que dans son attribut title. Ignoré sans Chromium.
"""

import json
import unittest

from aiohttp.test_utils import TestServer

from uidriver import CHROMIUM, Browser, make_app

STATE = """(() => {
    const b = document.getElementById('text-open');
    return JSON.stringify({ariaDisabled: b.getAttribute('aria-disabled'), disabled: b.disabled,
        opacity: getComputedStyle(b).opacity, dialogOpen: document.getElementById('text-dialog').open});
})()"""


@unittest.skipUnless(CHROMIUM, "Chromium is required")
class TextButtonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = TestServer(make_app())
        await self.server.start_server()
        self.browser = Browser()
        await self.browser.start()
        await self.browser.open(f"http://127.0.0.1:{self.server.port}/?token=player", 1280, 800)
        await self.browser.wait_for("document.getElementById('track-title').textContent !== ''")
        await self.browser.evaluate("document.querySelector('[data-tab=remote]').click()")

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.server.close()

    async def test_tapping_the_greyed_button_shows_the_reason_and_opens_nothing(self) -> None:
        state = json.loads(await self.browser.evaluate(STATE))
        self.assertEqual(state["ariaDisabled"], "true")
        self.assertLess(float(state["opacity"]), 0.5)  # toujours grisé
        await self.browser.evaluate("document.getElementById('text-open').click()")
        await self.browser.wait_for("document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('features.text_input')")
        self.assertFalse(json.loads(await self.browser.evaluate(STATE))["dialogOpen"])

"""Mise en page de l'onglet Lecture dans un vrai Chromium : rien d'essentiel ne doit être coupé, quelle que soit la taille.

Régression visée : les commandes du lecteur (précédent / lecture / suivant) et la barre de volume sortaient de la zone
visible quand la fenêtre était basse (tablette avec barre de navigateur, téléphone). Ignoré sans Chromium.
"""

import json
import unittest

from aiohttp.test_utils import TestServer

from uidriver import CHROMIUM, Browser, make_app

# (largeur, hauteur) en pixels CSS : tablettes avec ou sans barre de navigateur, portrait, téléphones
SIZES = [(960, 420), (960, 480), (960, 600), (1280, 640), (1280, 800), (800, 1280), (1024, 1366), (600, 960),
         (412, 915), (412, 800), (390, 844), (360, 780), (360, 640)]

# Un élément est « visible » s'il est entièrement dans le rectangle qui le contient
VISIBLE = """(() => {
    const inside = (el, box) => { const r = el.getBoundingClientRect(); return r.height > 0 && r.top >= box.top - 1 && r.bottom <= box.bottom + 1; };
    const byId = (id) => document.getElementById(id);
    const zone = document.querySelector('#panel-player .now-playing').getBoundingClientRect();
    const panels = document.querySelector('.panels').getBoundingClientRect();
    const result = {};
    for (const id of ['btn-prev', 'btn-play', 'btn-next', 'seek']) result[id] = inside(byId(id), zone);
    result['volume-bar'] = inside(document.querySelector('.volume-bar'), panels);
    result['master-volume'] = inside(byId('master-volume'), panels);
    return JSON.stringify(result);
})()"""

LAST_CARD = """(() => {
    const zone = document.querySelector('#panel-player .empty');
    const box = zone.getBoundingClientRect();
    const title = document.getElementById('last-title').getBoundingClientRect();
    const art = document.querySelector('.last-art').getBoundingClientRect();
    return JSON.stringify({title: title.top >= box.top - 1 && title.bottom <= box.bottom + 1, art: art.top >= box.top - 1});
})()"""


@unittest.skipUnless(CHROMIUM, "Chromium is required")
class PlayerLayoutTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = TestServer(make_app())
        await self.server.start_server()
        self.browser = Browser()
        await self.browser.start()

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.server.close()

    def url(self, scenario: str) -> str:
        return f"http://127.0.0.1:{self.server.port}/?token={scenario}"

    async def test_player_controls_and_volume_stay_visible(self) -> None:
        failures = []
        for width, height in SIZES:
            await self.browser.open(self.url("player"), width, height)
            await self.browser.wait_for("document.getElementById('track-title') && document.getElementById('track-title').textContent !== ''")
            visible = json.loads(await self.browser.evaluate(VISIBLE))
            failures += [f"{width}x{height}: {name} coupé" for name, ok in visible.items() if not ok]
        self.assertEqual(failures, [])

    async def test_last_opened_card_is_not_cut_off(self) -> None:
        failures = []
        for width, height in SIZES:
            await self.browser.open(self.url("last"), width, height)
            await self.browser.wait_for("document.getElementById('last-title') && document.getElementById('last-title').textContent !== ''")
            visible = json.loads(await self.browser.evaluate(LAST_CARD))
            failures += [f"{width}x{height}: {name} coupé" for name, ok in visible.items() if not ok]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()

"""Boutons de souris maintenus (bascule) dans la télécommande, dans un vrai Chromium.

Les deux boutons occupent les coins libres du pavé directionnel, de part et d'autre de la flèche haut. Ignoré sans
Chromium."""

import asyncio
import json
import unittest
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from uidriver import CHROMIUM, Browser, make_app, state

POSITIONS = """(() => {
    const rect = (selector) => { const r = document.querySelector(selector).getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
    const reachable = (selector) => { const r = document.querySelector(selector).getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return hit !== null && hit.closest(selector) !== null; };
    return JSON.stringify({lmb: rect('[data-hold=left]'), rmb: rect('[data-hold=right]'), up: rect('.dpad-btn.up'), ok: rect('.dpad-btn.ok'),
        lmbReachable: reachable('[data-hold=left]'), rmbReachable: reachable('[data-hold=right]'), vw: innerWidth, vh: innerHeight});
})()"""


def pressed(name: str) -> str:
    return f"document.querySelector('[data-hold={name}]').getAttribute('aria-pressed')"


@unittest.skipUnless(CHROMIUM, "Chromium is required")
class MouseHoldTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.held: set[str] = set()
        self.server = TestServer(make_app(self.on_message))
        await self.server.start_server()
        self.browser = Browser()
        await self.browser.start()
        await self.browser.open(f"http://127.0.0.1:{self.server.port}/?token=player", 1280, 800)
        await self.browser.wait_for("document.getElementById('track-title') && document.getElementById('track-title').textContent !== ''")
        await self.browser.evaluate("document.querySelector('[data-tab=remote]').click()")

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.server.close()

    async def on_message(self, message: dict[str, Any], socket: web.WebSocketResponse) -> None:
        """Comme le serveur : mémorise les boutons maintenus et renvoie l'état."""
        self.received.append(message)
        if message.get("action") == "button":
            (self.held.add if message["pressed"] else self.held.discard)(message["button"])
            await socket.send_json(self.state())

    def state(self) -> dict[str, Any]:
        current = state("player")
        current["input"] = {"available": True, "reason": "", "keys": [], "buttons": sorted(self.held),
                            "text": {"available": False, "reason": "disabled in config (features.text_input)"}}
        return current

    async def click(self, name: str) -> None:
        await self.browser.evaluate(f"document.querySelector('[data-hold={name}]').click()")

    async def buttons_sent(self, at_least: int = 0) -> list[tuple[str, bool]]:
        """Messages `button` reçus par le serveur. Le trajet navigateur -> serveur est asynchrone : on attend qu'il y en
        ait au moins ``at_least`` avant de les lire."""
        sent: list[tuple[str, bool]] = []
        for _ in range(60):
            sent = [(m["button"], m["pressed"]) for m in self.received if m.get("action") == "button"]
            if len(sent) >= at_least:
                break
            await asyncio.sleep(0.05)
        return sent

    async def test_buttons_sit_on_each_side_of_the_up_arrow(self) -> None:
        position = json.loads(await self.browser.evaluate(POSITIONS))
        left, right, up = position["lmb"], position["rmb"], position["up"]
        self.assertLessEqual(left["right"], up["left"] + 1)  # à gauche de la flèche haut
        self.assertGreaterEqual(right["left"], up["right"] - 1)  # à droite
        self.assertAlmostEqual(left["top"], up["top"], delta=1)  # même rangée
        self.assertAlmostEqual(right["top"], up["top"], delta=1)
        self.assertLessEqual(left["bottom"], position["ok"]["top"] + 1)  # au-dessus de la rangée du milieu
        self.assertGreaterEqual(left["height"], 44)  # assez grands pour un doigt
        self.assertTrue(position["lmbReachable"] and position["rmbReachable"])  # rien ne les recouvre

    async def test_tapping_toggles_the_button_on_then_off(self) -> None:
        self.assertEqual(await self.browser.evaluate(pressed("left")), "false")
        await self.click("left")
        self.assertEqual(await self.browser.evaluate(pressed("left")), "true")  # retour immédiat
        await self.click("left")
        self.assertEqual(await self.browser.evaluate(pressed("left")), "false")
        self.assertEqual(await self.buttons_sent(2), [("left", True), ("left", False)])

    async def test_the_two_buttons_are_independent(self) -> None:
        await self.click("left")
        await self.click("right")
        self.assertEqual(
            (await self.browser.evaluate(pressed("left")), await self.browser.evaluate(pressed("right"))), ("true", "true")
        )
        await self.click("left")
        self.assertEqual(
            (await self.browser.evaluate(pressed("left")), await self.browser.evaluate(pressed("right"))), ("false", "true")
        )

    async def test_leaving_the_remote_tab_releases_the_buttons(self) -> None:
        await self.click("left")
        await self.click("right")
        await self.browser.evaluate("document.querySelector('[data-tab=player]').click()")
        sent = await self.buttons_sent(4)
        self.assertCountEqual(sent[2:], [("left", False), ("right", False)])
        await self.browser.evaluate("document.querySelector('[data-tab=remote]').click()")
        self.assertEqual(await self.browser.evaluate(pressed("left")), "false")

    async def test_a_hidden_page_releases_the_buttons(self) -> None:
        await self.click("right")
        await self.browser.evaluate(
            "Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});"
            "document.dispatchEvent(new Event('visibilitychange'))"
        )
        self.assertEqual((await self.buttons_sent(2))[-1], ("right", False))
        self.assertEqual(await self.browser.evaluate(pressed("right")), "false")

    async def test_nothing_is_sent_when_nothing_is_held(self) -> None:
        await self.browser.evaluate("document.querySelector('[data-tab=player]').click()")
        await self.browser.evaluate("document.querySelector('[data-tab=remote]').click()")
        await asyncio.sleep(0.3)  # laisse à un éventuel message le temps d'arriver
        self.assertEqual(await self.buttons_sent(), [])


if __name__ == "__main__":
    unittest.main()

"""Tests de la page des QR codes : contenu, absence du jeton en clair, droits du fichier, choix des codes."""

import base64
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediadeck.__main__ import access_cards
from mediadeck.config import parse_config
from mediadeck.qrpage import QrCard, build_page, qr_svg, write_page

TOKEN = "secret-token-" + "x" * 20
LAN_IP = "192.168.1.104"
TLS = {"tls_cert": "/c.pem", "tls_key": "/k.key", "tls_ca": "/ca.crt"}


def cards_for(**server: object) -> list[QrCard]:
    return access_cards(parse_config({"server": {"token": TOKEN, "advertise_host": LAN_IP, **server}}))


class AccessCardsTest(unittest.TestCase):
    def test_plain_http_only_by_default(self) -> None:
        cards = cards_for()
        self.assertEqual([card.kind for card in cards], ["http"])
        self.assertEqual(cards[0].url, f"http://{LAN_IP}:8765/?token={TOKEN}")

    def test_https_only_with_tls(self) -> None:
        cards = cards_for(**TLS)
        self.assertEqual([card.kind for card in cards], ["https", "ca"])
        self.assertEqual(cards[0].url, f"https://{LAN_IP}:8765/?token={TOKEN}")
        self.assertEqual(cards[1].url, f"https://{LAN_IP}:8765/ca.crt")

    def test_both_modes_fetch_the_ca_over_http(self) -> None:
        cards = cards_for(**TLS, http_port=8766)
        self.assertEqual([card.kind for card in cards], ["https", "http", "ca"])
        self.assertEqual(cards[1].url, f"http://{LAN_IP}:8766/?token={TOKEN}")
        self.assertEqual(cards[2].url, f"http://{LAN_IP}:8766/ca.crt")

    def test_displayed_address_never_holds_the_token(self) -> None:
        for card in cards_for(**TLS, http_port=8766):
            self.assertNotIn(TOKEN, card.address)


@unittest.skipUnless(shutil.which("qrencode"), "qrencode is required")
class QrPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cards = cards_for(**TLS, http_port=8766)

    def test_page_is_self_contained_and_keeps_the_token_out_of_the_text(self) -> None:
        page = build_page(self.cards, "en")
        self.assertEqual(page.count('class="card"'), 3)
        self.assertNotIn(TOKEN, page)
        # Aucune ressource externe : seules les images en data: URI
        self.assertNotRegex(page, r"(src|href)=\"(?!data:)")
        self.assertIn("Unsecured (HTTP)", page)
        self.assertIn(f"http://{LAN_IP}:8766", page)

    def test_each_qr_code_encodes_its_url(self) -> None:
        page = build_page(self.cards, "en")
        images = re.findall(r"base64,([A-Za-z0-9+/=]+)\"", page)
        self.assertEqual([base64.b64decode(image) for image in images], [qr_svg(card.url) for card in self.cards])

    def test_qr_svg_has_no_anti_aliasing_seams(self) -> None:
        self.assertIn(b'shape-rendering="crispEdges"', qr_svg("http://x"))

    def test_language_and_escaping(self) -> None:
        card = QrCard("http", "http://<script>", "http://x")
        french = build_page([card], "fr")
        self.assertIn("Non sécurisé (HTTP)", french)
        self.assertIn('lang="fr"', french)
        self.assertNotIn("<script>", french)
        self.assertIn("&lt;script&gt;", french)

    def test_written_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as runtime, mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": runtime}):
            path = write_page(self.cards, "en")
            path.chmod(0o644)  # un fichier précédent aux droits trop larges est resserré à la réécriture
            path = write_page(self.cards, "en")
            self.assertEqual(path.parent, Path(runtime) / "mediadeck")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
            self.assertIn("MediaDeck", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

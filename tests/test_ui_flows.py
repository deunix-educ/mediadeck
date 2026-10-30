"""Parcours de l'interface dans un vrai Chromium, face au vrai serveur isolé (voir stack.py).

Onglet Médias (dossiers, fil d'Ariane, filtres, recherche, ouverture), lanceurs (mise à jour en direct et lancements
réels), page /admin (formulaires, ordre, suppression, lecteurs de fichiers) : chaque lancement est observé dans le
journal des faux programmes (``gio``, ``firefox``, commande de lanceur). Aucune exception JavaScript n'est tolérée.
Ignoré sans Chromium ni dbus-daemon.
"""

import asyncio
import json
import os
import shutil
import subprocess
import unittest
import warnings
from collections.abc import Callable
from typing import Any

from stack import DBUS_DAEMON, TOKEN, TRICKY_NAME, Stack
from uidriver import CHROMIUM, Browser

TILE_LABEL = ":scope > span"
# Contenu du dossier Musique : script.mp3 est un lien vers un dossier, donc affiché (et navigable) comme un dossier
MUSIQUE = [("dir", "Albums"), ("dir", "script.mp3"), ("audio", TRICKY_NAME)]


def js(value: Any) -> str:
    return json.dumps(value)


@unittest.skipUnless(CHROMIUM and DBUS_DAEMON, "Chromium and dbus-daemon are required")
class UiCase(unittest.IsolatedAsyncioTestCase):
    libraries: tuple[str, ...] = ("Musique", "Videos")
    thumbnails = False

    async def asyncSetUp(self) -> None:
        warnings.filterwarnings("ignore", message="unclosed <socket", category=ResourceWarning)
        self.stack = Stack(self.libraries, self.thumbnails)
        await self.stack.start()
        self.addAsyncCleanup(self.stack.close)
        self.browser = Browser()
        await self.browser.start()
        self.addAsyncCleanup(self.browser.close)
        self.addCleanup(lambda: self.assertEqual(self.browser.exceptions, [], "JavaScript exceptions"))

    # ------------------------------------------------------------------
    # Outils
    # ------------------------------------------------------------------

    async def open_remote(self) -> None:
        await self.browser.open(self.stack.url(f"/?token={TOKEN}"), 1280, 800)
        await self.browser.wait_for("document.querySelector('#tabbar') !== null && document.body.dataset.ready !== 'x'")

    async def open_admin(self) -> None:
        await self.open_remote()  # mémorise le jeton dans le stockage de l'origine
        await self.browser.open(self.stack.url("/admin"), 1280, 800)
        await self.browser.wait_for("document.querySelectorAll('#admin-list .admin-item').length > 0")

    async def select_tab(self, name: str) -> None:
        await self.browser.wait_for(f"!document.querySelector('[data-tab={name}]').hidden")
        await self.browser.evaluate(f"document.querySelector('[data-tab={name}]').click()")

    async def until(self, condition: Callable[[], Any], seconds: float = 5.0) -> bool:
        for _ in range(int(seconds / 0.05)):
            if condition():
                return True
            await asyncio.sleep(0.05)
        return False

    async def api(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        response = await self.stack.client.request(method, path, headers=headers, **kwargs)
        return response.status, (await response.json() if response.content_type == "application/json" else None)

    async def tiles(self) -> list[tuple[str, str]]:
        raw = await self.browser.evaluate(
            f"JSON.stringify([...document.querySelectorAll('#media-grid .media-tile')]"
            f".map((tile) => [tile.dataset.kind, tile.querySelector('{TILE_LABEL}').textContent]))"
        )
        return [tuple(item) for item in json.loads(raw)]

    async def wait_tiles(self, expected: list[tuple[str, str]]) -> None:
        for _ in range(100):
            if await self.tiles() == expected:
                return
            await asyncio.sleep(0.05)
        self.assertEqual(await self.tiles(), expected)

    async def click_tile(self, name: str) -> None:
        await self.wait_tile(name)
        await self.browser.evaluate(
            f"[...document.querySelectorAll('#media-grid .media-tile')]"
            f".find((tile) => tile.querySelector('{TILE_LABEL}').textContent === {js(name)}).click()"
        )

    async def wait_tile(self, name: str) -> None:
        for _ in range(100):
            if name in [label for _kind, label in await self.tiles()]:
                return
            await asyncio.sleep(0.05)
        self.assertIn(name, [label for _kind, label in await self.tiles()])


class MediaTabTest(UiCase):
    async def crumbs(self) -> list[str]:
        raw = await self.browser.evaluate(
            "JSON.stringify([...document.querySelectorAll('#media-crumbs .crumb')].map((crumb) => crumb.textContent))"
        )
        return json.loads(raw)

    async def test_browse_filter_and_open(self) -> None:
        await self.open_remote()
        await self.select_tab("media")
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])
        self.assertTrue(await self.browser.evaluate("document.getElementById('media-up').disabled"))

        await self.click_tile("Musique")
        await self.wait_tiles(MUSIQUE)  # ni .secret.mp3 (caché) ni notes.sh (extension hors liste)
        crumbs = await self.crumbs()
        self.assertEqual(crumbs[1:], ["Musique"])
        self.assertTrue(await self.browser.evaluate("document.querySelector('#media-crumbs .crumb.current') !== null"))

        await self.click_tile("Albums")
        await self.wait_tiles([("audio", "morceau.mp3")])
        self.assertEqual((await self.crumbs())[1:], ["Musique", "Albums"])
        await self.browser.evaluate("document.getElementById('media-up').click()")
        await self.wait_tiles(MUSIQUE)
        await self.browser.evaluate("document.getElementById('media-up').click()")  # d'un dossier racine à la liste
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])

        # Filtre par type : les dossiers restent toujours affichés
        await self.click_tile("Videos")
        await self.wait_tiles([("dir", "Films"), ("video", "clip.mp4")])
        chips = "document.querySelectorAll('#media-filters .chip')"
        self.assertEqual(await self.browser.evaluate(f"{chips}.length"), 4)
        await self.browser.evaluate(f"{chips}[1].click()")  # son
        await self.wait_tiles([("dir", "Films")])
        await self.browser.evaluate(f"{chips}[3].click()")  # vidéo
        await self.wait_tiles([("dir", "Films"), ("video", "clip.mp4")])
        await self.browser.evaluate(f"{chips}[0].click()")  # tous

        # Ouverture d'un fichier : retour visuel, puis lancement sur le PC sans passer par un shell
        await self.browser.evaluate("document.getElementById('media-up').click()")
        await self.click_tile("Musique")
        await self.wait_tiles(MUSIQUE)
        await self.click_tile(TRICKY_NAME)
        await self.browser.wait_for(f"document.querySelector('.toast')?.textContent.includes({js(TRICKY_NAME)})")
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("gio")))
        self.assertEqual(self.stack.tool_calls("gio"), [["open", str(self.stack.tricky)]])

    async def test_search_across_folders_and_opening_a_result(self) -> None:
        await self.open_remote()
        await self.select_tab("media")
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])

        async def type_query(text: str) -> None:
            await self.browser.evaluate(
                f"(() => {{ const box = document.getElementById('media-search'); box.value = {js(text)};"
                " box.dispatchEvent(new Event('input')); })()"
            )

        await type_query("m")  # trop court : aucune recherche
        await asyncio.sleep(0.6)
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])

        await type_query("morceau")
        await self.wait_tiles([("audio", "morceau.mp3"), ("video", "morceau-live.mp4")])
        captions = json.loads(
            await self.browser.evaluate(
                "JSON.stringify([...document.querySelectorAll('#media-grid .media-tile small')].map((s) => s.textContent))"
            )
        )
        self.assertEqual(captions, ["Musique / Albums", "Videos / Films"])  # le dossier d'origine de chaque résultat

        await self.click_tile("morceau-live.mp4")
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("gio")))
        self.assertEqual(self.stack.tool_calls("gio"), [["open", str(self.stack.videos / "Films" / "morceau-live.mp4")]])

        # Le bouton retour quitte la recherche
        await self.browser.evaluate("document.getElementById('media-up').click()")
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])
        self.assertEqual(await self.browser.evaluate("document.getElementById('media-search').value"), "")


    async def tri_folder(self) -> list[str]:
        """Dossier « Tri » : noms dans l'ordre alphabétique a, b, c ; dates et tailles dans trois autres ordres."""
        folder = self.stack.videos / "Tri"
        folder.mkdir()
        for name, size, mtime in (("a.mp4", 10, 1_700_000_300), ("b.mp4", 1000, 1_700_000_100), ("c.mp4", 100, 1_700_000_200)):
            (folder / name).write_bytes(b"x" * size)
            os.utime(folder / name, (mtime, mtime))
        return ["a.mp4", "b.mp4", "c.mp4"]

    async def files(self) -> list[str]:
        return [name for kind, name in await self.tiles() if kind != "dir"]

    async def choose_sort(self, value: str) -> None:
        await self.browser.evaluate(
            f"(() => {{ const box = document.getElementById('media-sort'); box.value = {js(value)};"
            " box.dispatchEvent(new Event('change')); })()"
        )

    async def test_sort_by_date_and_size_is_remembered(self) -> None:
        await self.tri_folder()
        await self.open_remote()
        await self.select_tab("media")
        await self.wait_tiles([("dir", "Musique"), ("dir", "Videos")])
        self.assertTrue(await self.browser.evaluate("document.getElementById('media-sort').hidden"))  # pas dans la liste des dossiers
        await self.click_tile("Videos")
        await self.wait_tiles([("dir", "Films"), ("dir", "Tri"), ("video", "clip.mp4")])
        await self.click_tile("Tri")
        await self.wait_tiles([("video", "a.mp4"), ("video", "b.mp4"), ("video", "c.mp4")])
        self.assertEqual(await self.browser.evaluate("document.getElementById('media-sort').value"), "name")

        await self.choose_sort("date")  # le plus récent d'abord
        self.assertEqual(await self.files(), ["a.mp4", "c.mp4", "b.mp4"])
        await self.choose_sort("size")  # le plus gros d'abord
        self.assertEqual(await self.files(), ["b.mp4", "c.mp4", "a.mp4"])

        # Les dossiers restent en tête quel que soit le tri
        await self.browser.evaluate("document.getElementById('media-up').click()")
        await self.wait_tiles([("dir", "Films"), ("dir", "Tri"), ("video", "clip.mp4")])

        # Le choix survit à un rechargement de la page
        await self.open_remote()
        await self.select_tab("media")
        await self.click_tile("Videos")
        await self.click_tile("Tri")
        await self.wait_tiles([("video", "b.mp4"), ("video", "c.mp4"), ("video", "a.mp4")])
        self.assertEqual(await self.browser.evaluate("document.getElementById('media-sort').value"), "size")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class ThumbnailTest(UiCase):
    libraries = ("Videos",)
    thumbnails = True

    def make_media(self) -> None:
        folder = self.stack.videos / "Medias"
        folder.mkdir()
        ffmpeg = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi"]
        subprocess.run([*ffmpeg, "-i", "testsrc=duration=1:size=96x64:rate=5", "-c:v", "mpeg4", str(folder / "film.mp4")], check=True)
        subprocess.run([*ffmpeg, "-i", "sine=duration=1", "-c:a", "flac", str(folder / "son.flac")], check=True)  # sans pochette

    async def test_video_gets_a_thumbnail_and_a_sound_without_cover_keeps_its_icon(self) -> None:
        await asyncio.to_thread(self.make_media)
        await self.open_remote()
        await self.select_tab("media")
        await self.click_tile("Medias")
        probe = (
            "JSON.stringify([...document.querySelectorAll('#media-grid .media-tile')].map((tile) => "
            f"[tile.querySelector('{TILE_LABEL}').textContent, tile.classList.contains('has-thumb'), "
            "tile.querySelector('img.thumb')?.naturalWidth ?? 0, tile.querySelector('svg') !== null]))"
        )

        async def tiles() -> dict[str, list[Any]]:
            return {name: rest for name, *rest in json.loads(await self.browser.evaluate(probe))}

        # La vidéo : une vraie image chargée depuis /thumb. Le son : l'image échoue (404), l'icône revient
        for _ in range(200):
            found = await tiles()
            if found.get("film.mp4", [False, 0])[1] > 0 and found.get("son.flac") == [False, 0, True]:
                break
            await asyncio.sleep(0.1)
        found = await tiles()
        self.assertTrue(found["film.mp4"][0] and found["film.mp4"][1] > 0, found)
        self.assertEqual(found["son.flac"], [False, 0, True], found)


class MediaDisabledTest(UiCase):
    libraries = ()

    async def test_tab_is_hidden_and_a_remembered_media_tab_falls_back_to_player(self) -> None:
        await self.open_remote()
        await self.browser.evaluate("localStorage.setItem('mediadeck.tab', 'media')")
        await self.open_remote()
        await self.browser.wait_for("document.querySelector('.tab.active')?.dataset.tab === 'player'")
        self.assertTrue(await self.browser.evaluate("document.querySelector('[data-tab=media]').hidden"))


class LauncherTest(UiCase):
    libraries = ("Musique",)

    async def labels(self) -> list[str]:
        raw = await self.browser.evaluate(
            "JSON.stringify([...document.querySelectorAll('#launcher-grid .launcher')].map((b) => b.textContent.trim()))"
        )
        return json.loads(raw)

    async def wait_labels(self, expected: list[str]) -> None:
        for _ in range(100):
            if await self.labels() == expected:
                return
            await asyncio.sleep(0.05)
        self.assertEqual(await self.labels(), expected)

    async def press(self, index: int) -> None:
        await self.browser.evaluate(f"document.querySelectorAll('#launcher-grid .launcher')[{index}].click()")

    async def test_launchers_run_for_real_and_the_grid_follows_the_admin(self) -> None:
        await self.open_remote()
        await self.select_tab("launchers")
        await self.wait_labels(["Recorder"])
        await self.press(0)
        await self.browser.wait_for("document.querySelector('.toast') !== null")
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("recorder")))
        self.assertEqual(self.stack.tool_calls("recorder"), [["--fs", "$(reboot)"]])  # pas de shell

        # Ajouts faits ailleurs (page /admin, autre tablette) : la grille se met à jour sans recharger
        app = {"label": "App", "icon": "app", "kind": "app", "spec": {"desktop_id": "fake.desktop"}}
        site: dict[str, Any] = {"label": "Site", "icon": "globe", "kind": "web"}
        site["spec"] = {"url": "https://example.org/", "browser": "firefox", "fullscreen": False}
        for body in (app, site):
            status, _ = await self.api("POST", "/api/launchers", json=body)
            self.assertEqual(status, 201)
        await self.wait_labels(["Recorder", "App", "Site"])

        await self.press(1)
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("gio")))
        self.assertEqual(self.stack.tool_calls("gio"), [["launch", str(self.stack.desktop)]])
        await self.press(2)
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("firefox")))
        self.assertEqual(self.stack.tool_calls("firefox"), [["--new-window", "https://example.org/"]])

        status, _ = await self.api("DELETE", "/api/launchers/rec")
        self.assertEqual(status, 204)
        await self.wait_labels(["App", "Site"])


class AdminPageTest(UiCase):
    libraries = ("Musique",)

    async def titles(self) -> list[str]:
        raw = await self.browser.evaluate(
            "JSON.stringify([...document.querySelectorAll('#admin-list .admin-item-title span:first-child')]"
            ".map((span) => span.textContent))"
        )
        return json.loads(raw)

    async def wait_titles(self, expected: list[str]) -> None:
        for _ in range(100):
            if await self.titles() == expected:
                return
            await asyncio.sleep(0.05)
        self.assertEqual(await self.titles(), expected)

    async def action(self, index: int, button: int) -> None:
        """Boutons d'une ligne : 0 monter, 1 descendre, 2 modifier, 3 supprimer."""
        await self.browser.evaluate(
            f"document.querySelectorAll('#admin-list .admin-item')[{index}]"
            f".querySelectorAll('.admin-actions button')[{button}].click()"
        )

    async def fill(self, element: str, value: str, event: str = "input") -> None:
        await self.browser.evaluate(
            f"(() => {{ const el = document.getElementById({js(element)}); el.value = {js(value)};"
            f" el.dispatchEvent(new Event({js(event)})); }})()"
        )

    async def submit(self, form: str) -> None:
        await self.browser.evaluate(f"document.getElementById({js(form)}).requestSubmit()")

    async def server_launchers(self) -> list[dict[str, Any]]:
        _, data = await self.api("GET", "/api/launchers")
        return data["launchers"]

    async def test_edit_add_reorder_and_delete_launchers(self) -> None:
        await self.open_admin()
        await self.wait_titles(["Recorder"])

        # Une commande importée se renomme, mais sa commande reste verrouillée (allow_custom_command est faux)
        await self.action(0, 2)
        await self.browser.wait_for("document.getElementById('launcher-dialog').open")
        self.assertTrue(await self.browser.evaluate("document.getElementById('form-command').disabled"))
        await self.fill("form-label", "Enregistreur")
        await self.submit("launcher-form")
        await self.wait_titles(["Enregistreur"])
        (renamed,) = await self.server_launchers()
        self.assertEqual(renamed["spec"], {"argv": ["recorder", "--fs", "$(reboot)"]})

        # Un nom vide est refusé et le dialogue reste ouvert
        await self.browser.evaluate("document.getElementById('admin-add').click()")
        await self.browser.wait_for("document.getElementById('launcher-dialog').open")
        await self.submit("launcher-form")
        await self.browser.wait_for("!document.getElementById('form-error').hidden")
        self.assertTrue(await self.browser.evaluate("document.getElementById('launcher-dialog').open"))

        # Ajout d'un lanceur web
        await self.fill("form-label", "Site")
        await self.fill("form-url", "https://example.org/")
        await self.fill("form-browser", "firefox", "change")
        await self.submit("launcher-form")
        await self.wait_titles(["Enregistreur", "Site"])
        self.assertFalse(await self.browser.evaluate("document.getElementById('launcher-dialog').open"))

        # Ordre, puis suppression (après confirmation)
        await self.action(1, 0)
        await self.wait_titles(["Site", "Enregistreur"])
        self.assertEqual([item["label"] for item in await self.server_launchers()], ["Site", "Enregistreur"])
        await self.browser.evaluate("window.confirm = () => true")
        await self.action(0, 3)
        await self.wait_titles(["Enregistreur"])
        self.assertEqual([item["id"] for item in await self.server_launchers()], ["rec"])

    async def test_media_opener_set_on_admin_is_used_when_opening_a_file(self) -> None:
        await self.open_admin()
        await self.browser.wait_for("document.querySelectorAll('#opener-list .admin-item').length === 3")
        await self.browser.evaluate("document.querySelectorAll('#opener-list .admin-item')[0].querySelector('button').click()")
        await self.browser.wait_for("document.getElementById('opener-dialog').open")
        # Les commandes brutes restent interdites : l'option est désactivée
        self.assertTrue(
            await self.browser.evaluate("[...document.getElementById('opener-kind').options].find((o) => o.value === 'command').disabled")
        )
        await self.fill("opener-kind", "app", "change")
        await self.browser.wait_for("[...document.getElementById('opener-app').options].some((o) => o.value === 'fake.desktop')")
        await self.fill("opener-app", "fake.desktop", "change")
        await self.submit("opener-form")
        await self.browser.wait_for("!document.getElementById('opener-dialog').open")
        _, data = await self.api("GET", "/api/media/openers")
        audio = next(opener for opener in data["openers"] if opener["type"] == "audio")
        self.assertEqual((audio["kind"], audio["spec"]), ("app", {"desktop_id": "fake.desktop"}))

        # Un seul dossier : l'onglet Médias l'ouvre directement ; le fichier son passe par l'application choisie
        await self.open_remote()
        await self.select_tab("media")
        await self.browser.wait_for(
            f"[...document.querySelectorAll('#media-grid .media-tile')].some((t) => t.textContent.includes({js(TRICKY_NAME)}))"
        )
        await self.browser.evaluate(
            f"[...document.querySelectorAll('#media-grid .media-tile')]"
            f".find((t) => t.textContent.includes({js(TRICKY_NAME)})).click()"
        )
        self.assertTrue(await self.until(lambda: self.stack.tool_calls("gio")))
        self.assertEqual(self.stack.tool_calls("gio"), [["launch", str(self.stack.desktop), str(self.stack.tricky)]])


if __name__ == "__main__":
    unittest.main()

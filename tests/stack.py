"""Environnement isolé partagé par les tests de bout en bout : vrai serveur, bus D-Bus privé, faux programmes.

Rien ne touche au bureau de l'utilisateur : le bus de session, les dossiers XDG et le PATH sont remplacés, et les
programmes lancés (``gio``, ``pactl``, ``firefox``, une commande de lanceur) sont des scripts qui journalisent leurs
arguments dans ``tools.log``. Le serveur est un vrai ``MediaDeckServer`` servi par ``aiohttp.test_utils``.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from mediadeck import server as server_module
from mediadeck.config import parse_config
from mediadeck.launchers import LauncherStore
from mediadeck.server import MediaDeckServer

DBUS_DAEMON = shutil.which("dbus-daemon")
TOKEN = "t" * 32
TRICKY_NAME = "été ♪ $(reboot).mp3"  # accents, symboles et syntaxe de shell : doit arriver tel quel à gio
# PNG 1×1 valide
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002"
    "00010005fe02fe0a2d2d1b0000000049454e44ae426082"
)
TOOLS = ("gio", "pactl", "recorder", "firefox")

# Programme factice : ajoute une ligne JSON {"tool": nom, "argv": [...]} au journal puis se termine
# (``pactl`` répond en plus aux requêtes de lecture ; ``subscribe`` reste ouvert comme le vrai)
FAKE_TOOL = """#!{python}
import json, os, sys, time
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["MEDIADECK_FAKE_LOG"], "a") as log:
    log.write(json.dumps({{"tool": tool, "argv": args}}) + "\\n")
if tool == "pactl":
    if args[-2:] == ["list", "sinks"]:
        print(json.dumps([{{"name": "fake", "description": "Fake output", "mute": False,
                           "volume": {{"front-left": {{"value_percent": "50%"}}}}}}]))
    elif args == ["get-default-sink"]:
        print("fake")
    elif args == ["subscribe"]:
        time.sleep(3600)
"""

# Bus de test sans dossier de services : aucun programme du système (kdeconnect, portails, kwallet…) ne peut y être activé
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
    <type>session</type>
    <listen>unix:tmpdir=/tmp</listen>
    <auth>EXTERNAL</auth>
    <policy context="default">
        <allow send_destination="*" eavesdrop="true"/>
        <allow eavesdrop="true"/>
        <allow own="*"/>
    </policy>
</busconfig>
"""

DESKTOP_ENTRY = "[Desktop Entry]\nType=Application\nName=Fake application\nExec=fake-app\n"


class Stack:
    """Serveur réel + bibliothèque de médias + faux programmes. ``await start()``, puis ``await close()``."""

    def __init__(self, libraries: tuple[str, ...] = ("Musique",)) -> None:
        # libraries : dossiers de médias à créer et à déclarer ; vide = onglet Médias désactivé
        self.libraries = libraries
        self.address = ""

    async def start(self) -> None:
        assert DBUS_DAEMON
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name).resolve()

        # Faux programmes en tête du PATH, journal commun
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool in TOOLS:
            script = bin_dir / tool
            script.write_text(FAKE_TOOL.format(python=sys.executable))
            script.chmod(0o755)
        self.log_path = tmp / "tools.log"
        self.log_path.touch()
        (tmp / "data" / "applications").mkdir(parents=True)
        self.desktop = tmp / "data" / "applications" / "fake.desktop"
        self.desktop.write_text(DESKTOP_ENTRY)

        # Bibliothèque : un nom piégé, un fichier caché, un lien vers un dossier, une extension hors liste
        self.root = tmp / "Musique"
        self.videos = tmp / "Videos"
        if "Musique" in self.libraries:
            (self.root / "Albums").mkdir(parents=True)
            (self.root / "Albums" / "morceau.mp3").write_bytes(b"audio")
            self.tricky = self.root / TRICKY_NAME
            self.tricky.write_bytes(b"audio")
            (self.root / ".secret.mp3").write_bytes(b"audio")
            (self.root / "script.mp3").symlink_to(self.root / "Albums")  # un dossier, pas un fichier audio
            (self.root / "notes.sh").write_text("#!/bin/sh\n")
        if "Videos" in self.libraries:
            (self.videos / "Films").mkdir(parents=True)
            (self.videos / "Films" / "morceau-live.mp4").write_bytes(b"video")
            (self.videos / "clip.mp4").write_bytes(b"video")
        self.art_file = tmp / "cover.png"
        self.art_file.write_bytes(PNG)

        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "MEDIADECK_FAKE_LOG": str(self.log_path),
            "XDG_DATA_HOME": str(tmp / "data"),
            "XDG_CACHE_HOME": str(tmp / "cache"),
        }
        self._environ = mock.patch.dict(os.environ, env)
        self._environ.start()

        bus_config = tmp / "bus.conf"
        bus_config.write_text(BUS_CONFIG)
        self._daemon = subprocess.Popen(
            [DBUS_DAEMON, f"--config-file={bus_config}", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self._daemon.stdout is not None
        self.address = self._daemon.stdout.readline().strip()
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.address

        config = parse_config(
            {
                "server": {"token": TOKEN, "host": "127.0.0.1"},
                "features": {"input": False, "power": False},
                "media": {"roots": [str(tmp / name) for name in self.libraries], "thumbnails": False},
                "launchers": [{"id": "rec", "label": "Recorder", "command": ["recorder", "--fs", "$(reboot)"]}],
            }
        )
        store = LauncherStore(tmp / "launchers.db")
        with mock.patch.object(server_module, "LauncherStore", lambda: store):
            self.deck = MediaDeckServer(config)
        self.client = TestClient(TestServer(self.deck.build_app()))
        await self.client.start_server()

    @property
    def port(self) -> int:
        return self.client.server.port  # type: ignore[return-value]  # TestServer.port est None avant start_server

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def close(self) -> None:
        await self.client.close()  # arrête le serveur (on_shutdown déconnecte son bus)
        self._daemon.terminate()
        self._daemon.wait()
        assert self._daemon.stdout is not None
        self._daemon.stdout.close()
        self._environ.stop()
        self._tmp.cleanup()

    def tool_calls(self, tool: str) -> list[list[str]]:
        lines = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [line["argv"] for line in lines if line["tool"] == tool]

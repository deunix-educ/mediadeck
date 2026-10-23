"""Volume et sorties audio via pactl (compatible PipeWire / pipewire-pulse)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def parse_sinks(raw_json: str) -> list[dict[str, Any]]:
    """Extrait nom, description, volume moyen (0..1+) et mute de `pactl -f json list sinks`."""
    sinks: list[dict[str, Any]] = []
    for sink in json.loads(raw_json):
        channels = sink.get("volume", {})
        percents: list[float] = []
        for channel in channels.values():
            text = str(channel.get("value_percent", "0%")).rstrip("%").strip()
            try:
                percents.append(float(text))
            except ValueError:
                continue
        volume = (sum(percents) / len(percents) / 100.0) if percents else 0.0
        sinks.append(
            {
                "name": sink.get("name", ""),
                "description": sink.get("description") or sink.get("name", ""),
                "volume": round(volume, 3),
                "muted": bool(sink.get("mute", False)),
            }
        )
    return sinks


class AudioManager:
    """Suit les sorties via `pactl subscribe` et applique les commandes."""

    def __init__(self, max_volume: float, on_change: Callable[[], None]) -> None:
        self._max_volume = max_volume
        self._on_change = on_change
        self._pactl = shutil.which("pactl")
        self._sinks: list[dict[str, Any]] = []
        self._default = ""
        self._tasks: list[asyncio.Task[None]] = []
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return self._pactl is not None

    async def start(self) -> None:
        if not self.available:
            log.warning("pactl not found: audio control disabled (apt install pulseaudio-utils)")
            return
        await self.refresh()
        self._tasks.append(asyncio.create_task(self._watch()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

    async def _run(self, *args: str) -> str:
        assert self._pactl is not None
        process = await asyncio.create_subprocess_exec(
            self._pactl,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or f"pactl {args[0]} failed")
        return stdout.decode()

    async def refresh(self) -> None:
        try:
            self._sinks = parse_sinks(await self._run("-f", "json", "list", "sinks"))
            self._default = (await self._run("get-default-sink")).strip()
        except (RuntimeError, json.JSONDecodeError) as exc:
            log.warning("audio refresh failed: %s", exc)
            return
        self._on_change()

    def _schedule_refresh(self) -> None:
        # Anti-rebond : pactl subscribe émet des rafales d'événements
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        async def delayed() -> None:
            await asyncio.sleep(0.1)
            await self.refresh()

        self._refresh_task = asyncio.create_task(delayed())

    async def _watch(self) -> None:
        """Relance `pactl subscribe` si le serveur audio redémarre."""
        assert self._pactl is not None
        while True:
            process = await asyncio.create_subprocess_exec(
                self._pactl,
                "subscribe",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdout is not None
            try:
                async for line in process.stdout:
                    text = line.decode(errors="replace")
                    if "sink" in text or "server" in text:
                        self._schedule_refresh()
            except asyncio.CancelledError:
                process.kill()
                raise
            await process.wait()
            log.info("pactl subscribe exited, restarting in 2 s")
            await asyncio.sleep(2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "default": self._default,
            "max_volume": self._max_volume,
            "sinks": self._sinks,
        }

    def _check_sink(self, name: Any) -> str:
        if not isinstance(name, str) or name not in {sink["name"] for sink in self._sinks}:
            raise ValueError("unknown sink")
        return name

    async def execute(self, action: str, sink: Any, value: Any) -> None:
        if not self.available:
            raise RuntimeError("audio control unavailable")
        sink_name = self._check_sink(sink if sink is not None else self._default)
        match action:
            case "set_volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("numeric volume expected")
                volume = min(self._max_volume, max(0.0, float(value)))
                await self._run("set-sink-volume", sink_name, f"{round(volume * 100)}%")
            case "toggle_mute":
                await self._run("set-sink-mute", sink_name, "toggle")
            case "set_default":
                await self._run("set-default-sink", sink_name)
            case _:
                raise ValueError(f"unknown audio action '{action}'")

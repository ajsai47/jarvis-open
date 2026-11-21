"""Quiets other sound while Jarvis talks: mutes playing Dia tabs (through the Jarvis for Dia
extension) and pauses Spotify / Music, then puts back only what it changed."""
import asyncio
import subprocess

import settings
from browser_bridge import BRIDGE

PLAYERS = {"Spotify": "Spotify", "Music": "Music"}  # process name -> AppleScript app name
RESUME_AFTER_S = 1.2  # Jarvis pauses between sentences; don't let the music flicker back in


class Hush:
    def __init__(self) -> None:
        self.tabs: list[int] = []
        self.paused: list[str] = []
        self.on = False
        self._resume: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()

    def speaking(self, on: bool) -> None:
        if not settings.get("hush_media"):
            return
        loop = asyncio.get_running_loop()
        if self._resume:
            self._resume.cancel()
            self._resume = None
        if on:
            loop.create_task(self._quiet())
        else:
            self._resume = loop.call_later(RESUME_AFTER_S, lambda: loop.create_task(self._restore()))

    async def _quiet(self) -> None:
        async with self._lock:
            if self.on:
                return
            self.on = True
            if BRIDGE.connected:
                try:
                    self.tabs = list(await BRIDGE.call("hush") or [])
                except Exception:
                    self.tabs = []
            self.paused = await asyncio.to_thread(_pause_players)

    async def _restore(self) -> None:
        async with self._lock:
            if not self.on:
                return
            self.on = False
            tabs, self.tabs = self.tabs, []
            paused, self.paused = self.paused, []
            if tabs and BRIDGE.connected:
                try:
                    await BRIDGE.call("unhush", tabs=tabs)
                except Exception:
                    pass
            if paused:
                await asyncio.to_thread(_resume_players, paused)


def _running(name: str) -> bool:
    return subprocess.run(["pgrep", "-xq", name]).returncode == 0


def _osa(script: str) -> str:
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pause_players() -> list[str]:
    out = []
    for proc, app in PLAYERS.items():
        if _running(proc) and _osa(f'tell application "{app}" to if player state is playing then pause\n'
                                   f'tell application "{app}" to return player state as text') == "paused":
            out.append(app)
    return out


def _resume_players(apps: list[str]) -> None:
    for app in apps:
        _osa(f'tell application "{app}" to if player state is paused then play')


HUSH = Hush()

"""Screen look for Mac Copilot: capture one window (never the whole screen) so gpt-5.5 can see it.

Needs Screen Recording permission for the app that starts Mac Copilot. The picture is sent to
OpenAI only when the user asks about what's on their screen."""

import asyncio
import base64
import ctypes
import ctypes.util
import os
import sys
import tempfile

from agents import ToolOutputImage, function_tool

MAC = sys.platform == "darwin"
if MAC:
    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    _vp, _long = ctypes.c_void_p, ctypes.c_long
    _cg.CGWindowListCopyWindowInfo.restype = _vp
    _cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _cf.CFArrayGetCount.restype = _long
    _cf.CFArrayGetCount.argtypes = [_vp]
    _cf.CFArrayGetValueAtIndex.restype = _vp
    _cf.CFArrayGetValueAtIndex.argtypes = [_vp, _long]
    _cf.CFDictionaryGetValue.restype = _vp
    _cf.CFDictionaryGetValue.argtypes = [_vp, _vp]
    _cf.CFStringCreateWithCString.restype = _vp
    _cf.CFStringCreateWithCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [_vp, ctypes.c_char_p, _long, ctypes.c_uint32]
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [_vp, ctypes.c_int, _vp]
    _cf.CFRelease.argtypes = [_vp]
_UTF8, _SINT64 = 0x08000100, 4
_ON_SCREEN, _NO_DESKTOP = 1, 16


def _key(name: str) -> int:
    return _cf.CFStringCreateWithCString(None, name.encode(), _UTF8)


_K = {} if not MAC else {k: _key(k) for k in ("kCGWindowNumber", "kCGWindowOwnerName", "kCGWindowName",
                           "kCGWindowLayer", "kCGWindowAlpha")}


def _str(d: int, k: str) -> str:
    v = _cf.CFDictionaryGetValue(d, _K[k])
    buf = ctypes.create_string_buffer(1024)
    return buf.value.decode(errors="ignore") if v and _cf.CFStringGetCString(v, buf, 1024, _UTF8) else ""


def _num(d: int, k: str) -> int:
    v = _cf.CFDictionaryGetValue(d, _K[k])
    out = ctypes.c_int64(0)
    if v:
        _cf.CFNumberGetValue(v, _SINT64, ctypes.byref(out))
    return out.value


def _windows() -> list[tuple[int, str, str]]:
    """Normal on-screen windows, front to back: (window id, app name, window title)."""
    arr = MAC and _cg.CGWindowListCopyWindowInfo(_ON_SCREEN | _NO_DESKTOP, 0)
    if not arr:
        return []
    try:
        out = []
        for i in range(_cf.CFArrayGetCount(arr)):
            d = _cf.CFArrayGetValueAtIndex(arr, i)
            if _num(d, "kCGWindowLayer") == 0:
                out.append((_num(d, "kCGWindowNumber"), _str(d, "kCGWindowOwnerName"),
                            _str(d, "kCGWindowName")))
        return out
    finally:
        _cf.CFRelease(arr)


def front_note() -> str:
    """The window the user was in when they asked (app + title), so "what's this about?" has a "this"."""
    try:
        import subprocess
        front = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first '
                                "process whose frontmost is true"], capture_output=True, text=True, timeout=3).stdout.strip()
        # The top window on screen can belong to another display's app, so take the front app's own window.
        for _, app, title in [w for w in _windows() if w[1] == front] or [(0, front, "")] if front else _windows():
            if app in ("Mac Copilot", "Jarvis Notch", "Window Server", "Dock") or "Mac Copilot" in title \
                    or title.split(" - ")[0].strip() == "Jarvis":
                continue
            what = f"{app} — “{title[:200]}”" if title else app
            return (f"\n(Front window when asked: {what}. Use it only if the request is about what they're "
                    "looking at, like “this” or “here”.)")
    except Exception:
        pass
    return ""


def _pick(app_name: str) -> tuple[int, str, str] | None:
    for w in _windows():
        if app_name:
            if w[1].lower() == app_name.lower():
                return w
        elif "Mac Copilot" not in w[2] and w[1] not in ("Mac Copilot", "Window Server", "Dock"):
            return w
    return None


@function_tool
async def look_at_screen(app_name: str = "") -> ToolOutputImage | str:
    """Take a picture of one window so you can see it: the window the user is looking at (the
    frontmost one, skipping the Mac Copilot page), or the front window of app_name. Use only when
    the user asks about what's on their screen ("what am I looking at?", "summarise this")."""
    w = await asyncio.to_thread(_pick, app_name)
    if not w:
        return f"No open window found{' for ' + app_name if app_name else ''}."
    wid, owner, title = w
    path = os.path.join(tempfile.gettempdir(), f"mc_look_{wid}.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-o", "-t", "jpg", "-l", str(wid), path,
            stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode or not os.path.exists(path):
            return (f"Couldn't capture {owner}: {err.decode().strip()[:150]}. Mac Copilot may need "
                    "Screen Recording in System Settings → Privacy & Security.")
        await (await asyncio.create_subprocess_exec(
            "sips", "-Z", "1600", path, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)).wait()
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
    finally:
        if os.path.exists(path):
            os.remove(path)
    if not title:
        return (f"macOS only showed the desktop for {owner}: allow Mac Copilot in System Settings → "
                "Privacy & Security → Screen & System Audio Recording, then restart it.")
    return ToolOutputImage(image_url=f"data:image/jpeg;base64,{data}", detail="auto")


SCREEN_TOOLS = [look_at_screen]

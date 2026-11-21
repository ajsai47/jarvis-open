"""Website tools: Jarvis's own background tabs in the user's Dia, signed in as the user,
through the Jarvis for Dia extension. Each task keeps its own tab (in desktop_tools._ctx)."""
import re

from agents import function_tool

import desktop_tools as dt
from browser_bridge import BRIDGE


def _tab() -> int:
    tab = dt._ctx.get().get("tab")
    if not tab:
        raise RuntimeError("No tab yet: call web_open first.")
    return tab


async def _do(cmd: str, **args):
    try:
        return await BRIDGE.call(cmd, **args)
    except RuntimeError as e:
        return f"Error: {e}"


@function_tool
async def web_tabs() -> str:
    """List the tabs open in the user's Dia (tab id, title, url), to find a page they already have open."""
    r = await _do("tabs")
    if isinstance(r, str):
        return r
    return "\n".join(f"{t['tab']}: {t['title'][:80]} — {t['url'][:120]}" + (" (yours)" if t.get("jarvis") else "")
                     for t in r) or "No tabs open."


@function_tool
async def web_open(url: str = "", tab_id: int = 0) -> str:
    """Start website work in the user's Dia, in the background and signed in as them.
    url: open it in a new background tab of your own. tab_id: work in a tab the user already has open
    (from web_tabs) instead; only when they mean that page. Then call web_look."""
    if tab_id:
        dt._ctx.get()["tab"] = tab_id
        return f"Working in tab {tab_id}. Call web_look."
    if not re.match(r"https?://", url):
        url = "https://" + url
    r = await _do("open", url=url)
    if isinstance(r, str):
        return r
    dt._ctx.get()["tab"] = r["tab"]
    return f"Opened \"{r['title']}\" ({r['url']}) in a background tab. Call web_look."


@function_tool
async def web_go(url: str) -> str:
    """Go to another address in your tab."""
    if not re.match(r"https?://", url):
        url = "https://" + url
    try:
        r = await _do("go", tab=_tab(), url=url)
    except RuntimeError as e:
        return f"Error: {e}"
    return r if isinstance(r, str) else f"Now on \"{r['title']}\" ({r['url']})."


@function_tool
async def web_look(filter: str = "") -> str:
    """See the page in your tab: its title, address, and every button, link, field and heading as
    [ref] role "name". Refs change after every look. filter: only lines containing this text."""
    try:
        r = await _do("look", tab=_tab(), filter=filter)
    except RuntimeError as e:
        return f"Error: {e}"
    return r if isinstance(r, str) else f"{r['title']} — {r['url']}\n{r['elements']}"


@function_tool
async def web_read() -> str:
    """Read the text of the page in your tab."""
    try:
        r = await _do("read", tab=_tab())
    except RuntimeError as e:
        return f"Error: {e}"
    return r if isinstance(r, str) else f"{r['title']} — {r['url']}\n{r['text']}"


@function_tool
async def web_click(ref: str) -> str:
    """Click a button or link by its ref from the latest web_look. Look again afterwards."""
    try:
        return await _do("click", tab=_tab(), ref=ref)
    except RuntimeError as e:
        return f"Error: {e}"


@function_tool
async def web_type(ref: str, text: str, submit: bool = False) -> str:
    """Type into a field by its ref (replaces what's in it). submit=True presses Enter afterwards,
    which may send or submit: only after the user's yes when it sends anything."""
    try:
        return await _do("type", tab=_tab(), ref=ref, text=text, submit=submit)
    except RuntimeError as e:
        return f"Error: {e}"


@function_tool
async def web_press(key: str) -> str:
    """Press Enter, Tab, Escape, Backspace, ArrowDown or ArrowUp in your tab. Enter may send or submit."""
    try:
        return await _do("key", tab=_tab(), key=key)
    except RuntimeError as e:
        return f"Error: {e}"


@function_tool
async def web_scroll(down: bool = True) -> str:
    """Scroll your tab down (or up) by about a screen."""
    try:
        return await _do("scroll", tab=_tab(), dy=600 if down else -600)
    except RuntimeError as e:
        return f"Error: {e}"


WEB_TOOLS = [web_tabs, web_open, web_go, web_look, web_read, web_click, web_type, web_press, web_scroll]
WEB_READ = {"web_tabs", "web_open", "web_go", "web_look", "web_read", "web_click", "web_scroll"}
WEB_UNDOABLE = WEB_READ | {"web_type"}
WEB_RULE = """
Websites: do website work with the web_* tools. web_open gives you your own background tab in the
user's Dia, signed in as them; then web_look, web_click, web_type, web_read, and web_look again
after every change. Use web_tabs and web_open(tab_id=...) only when the user means a page they
already have open. If a web_* tool says Dia isn't connected, fall back to open_website and
look_at_app on Dia. To simply show the user a site, use open_website. Sending, posting or
submitting on a website follows the Safety rule: never press Send/Post/Submit or Enter to send
without the user's yes.
"""

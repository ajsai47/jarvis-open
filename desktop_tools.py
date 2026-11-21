"""Tools that let an OpenAI agent see and operate Mac apps through agent-desktop."""

import asyncio
import contextvars
import datetime
import json
import os
import plistlib
import subprocess
from functools import cache
from pathlib import Path

from agents import function_tool

HOME = Path.home()


def load_env() -> None:
    """Load environment values only from this checkout’s own .env file."""
    for env_file in (Path(__file__).with_name(".env"),):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


INSTRUCTIONS = """
You are Mac Copilot, a friendly assistant that operates apps on the user's Mac.
You see apps through their accessibility tree, not pixels.

How to work:
1. open_app if needed, then look_at_app to get a numbered list of controls (e.g. "e12 button 'Play'").
2. Act with click / type_text / press_key using those ids (e.g. "e12").
3. After acting, look_at_app or read_app_text again to confirm it worked. Never claim success without checking.
4. Ids expire whenever you call look_at_app again, so always use ids from your latest look.
5. Use look_at_app's `filter` argument to find things fast in big apps.
6. Match each id to its label carefully (e.g. find the line that says button '1' before clicking "1").
6b. Use the exact app name the user says, even if you don't recognize it (e.g. "Dia" is a
   web browser). Never swap in a different app. list_installed_apps shows what exists.
6c. For websites use open_website. To quit an app use quit_app (never cmd+q).
6d. You work in the background while the user keeps using their Mac: never try to bring
   apps to the front or take over the mouse/keyboard. If something can't be done this way,
   say so instead of forcing it. Some fields (often Slack and other Electron or Chromium apps)
   refuse background typing. Then retry click, type_text or press_key with take_focus=True
   without asking: that brings the app forward briefly and uses the real keyboard, so do the
   whole step in one go, check the result, and mention that you took focus.
6e. For texts / iMessage use recent_texts, read_texts and search_texts. They read Messages
   directly, without opening the app. To send one, use send_text (never type into the
   Messages window), and only after the user has said yes to the exact recipient and words.
   If one contact matches the name, use it without asking; ask only when several match.
   Ask for each send once: a yes to the exact draft is enough, never ask "Send it?" twice.
6f. For reminders use add_reminder and list_reminders (never the Reminders window).
6g. For files use find_files (Spotlight) and open_file.
6h. For the calendar use list_events and add_event (never the Calendar window).
6j. For Slack use slack_inbox (DMs and mentions to the user), slack_read, slack_search and
   slack_send. They use the Slack API as the user, with no Slack window, so prefer them to the
   Slack app. slack_send only after the user has said yes to the exact recipient and words.
6k. For email use email_inbox, search_email, read_email and send_email (Gmail, no window).
   send_email only after the user has said yes to the exact recipient, subject and words.
6l. For the user's AI agents (Grok bots, Codex threads, Devin sessions/threads) use
   agent_status, then agent_thread for one agent's latest messages. To tell an agent something,
   use message_agent with its id, only after the user has said yes to the exact words.
6i. When the user asks about what's on their screen ("what am I looking at?", "summarise
   this", "reply to this"), use look_at_screen; pass app_name if they name an app.
7. Before you reply, check the final result actually makes sense for the request
   (e.g. 12 x 12 should show 144). If it doesn't, clear it and redo the steps.

Safety: before anything that sends a message or email, deletes, buys, posts publicly,
or changes passwords/security settings, describe what you're about to do and ask the user
to confirm. Only continue after they say yes.

Keep replies short and upbeat, and say what you did.
"""

# Per-task agent-desktop state. Parallel helpers each set their own (session, agent id and
# latest snapshot), so their refs and cursors never mix.
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ad_ctx", default={"snap": ""})


async def _ad(*args: str, timeout: float = 30) -> dict:
    c = _ctx.get()
    if c.get("agent"):
        args = ("--session", c["session"], "--agent-id", c["agent"],
                *(a for a in args if a != "--headed"))
    proc = await asyncio.create_subprocess_exec(
        "agent-desktop", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": {"message": "agent-desktop timed out"}}
    try:
        return json.loads(out.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": out.decode()[:500]}}


def _err(d: dict) -> str:
    e = d.get("error") or {}
    return f"Failed: {e.get('code', '')} {e.get('message', '')} {e.get('suggestion', '')}".strip()


def _full_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("@"):
        return ref
    return f"@{_ctx.get()['snap']}:{ref}"


async def _window_id(app: str) -> str:
    d = await _ad("list-windows", "--app", app)
    wins = [w for w in d.get("data") or [] if w.get("accessible") and w.get("visible")]
    return wins[0]["id"] if wins else ""


def _flatten(tree: dict, only_refs: bool, filt: str) -> list[str]:
    lines: list[str] = []

    def walk(n: dict) -> None:
        name = n.get("name") or ""
        value = n.get("value") or ""
        ref = (n.get("ref_id") or "").split(":")[-1]
        if (ref or not only_refs) and (name or value or ref):
            text = f"{ref or '-'} {n.get('role')} {name!r}"
            if value and value != name:
                text += f" value={str(value)[:80]!r}"
            if not filt or filt.lower() in text.lower():
                lines.append(text)
        for c in n.get("children", []):
            walk(c)

    walk(tree)
    return lines


@function_tool
async def list_open_apps() -> str:
    """List the apps that are currently running on the Mac (Dock apps, not hidden helpers)."""
    d = await _ad("list-apps")
    if not d.get("ok"):
        return _err(d)
    apps = d.get("data") or {}
    apps = apps.get("apps", []) if isinstance(apps, dict) else apps
    shown = [a for a in apps if a.get("presentation") == "foreground"] or apps
    return ", ".join(sorted({a.get("name", "") for a in shown}))


async def _osa(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _frontmost() -> str:
    return await _osa('tell application "System Events" to get bundle identifier '
                      "of first process whose frontmost is true")


async def _bundle_id(app: str) -> str:
    return await _osa(f'id of application "{app}"')


@cache
def _default_browser() -> str:
    out = subprocess.run(
        ["defaults", "export", "com.apple.LaunchServices/com.apple.launchservices.secure", "-"],
        capture_output=True,
    ).stdout
    try:
        handlers = plistlib.loads(out).get("LSHandlers", [])
    except Exception:
        return ""
    return next((h.get("LSHandlerRoleAll", "") for h in handlers
                 if h.get("LSHandlerURLScheme") == "https"), "")


async def _watch_focus(prev: str, opened: str) -> None:
    for _ in range(15):
        await asyncio.sleep(0.2)
        if await _frontmost() == opened:
            await _osa(f'tell application id "{prev}" to activate')


_focus_watchers: set[asyncio.Task] = set()


def _give_focus_back(prev: str, opened: str) -> None:
    """Some apps (e.g. Slack) jump to the front when opened. For the next few seconds, if the
    app we opened takes the front, put the user's app back; leave any other switch alone
    since that's the user. Runs in the background so the action doesn't wait for it."""
    if not prev or not opened or prev == opened:
        return
    task = asyncio.create_task(_watch_focus(prev, opened))
    _focus_watchers.add(task)
    task.add_done_callback(_focus_watchers.discard)


def _full_screen_apps() -> list[str]:
    """Apps that have a window in full screen (each gets its own desktop/Space)."""
    out = subprocess.run(["defaults", "export", "com.apple.spaces", "-"],
                         capture_output=True).stdout
    try:
        monitors = (plistlib.loads(out).get("SpacesDisplayConfiguration", {})
                    .get("Management Data", {}).get("Monitors", []))
    except Exception:
        return []
    pids = {str(sp["pid"]) for m in monitors for sp in m.get("Spaces", [])
            if sp.get("type") == 4 and sp.get("pid")}
    if not pids:
        return []
    ps = subprocess.run(["ps", "-o", "comm=", "-p", ",".join(pids)],
                        capture_output=True, text=True).stdout
    names = set()
    for line in ps.splitlines():
        parts = Path(line.strip()).parts
        app = next((p[:-4] for p in parts if p.endswith(".app")), Path(line.strip()).name)
        names.add(app)
    return sorted(n for n in names if n)


def _no_window(app_name: str) -> str:
    full = _full_screen_apps()
    if app_name in full:
        return (f"{app_name} is in full screen, so its window is on its own desktop and can't be "
                f"read in the background. Tell the user to click into {app_name} and press "
                "Control + Command + F to leave full screen; don't retry.")
    if full:
        return (f"{app_name}'s window is hidden because these apps are in full screen: "
                f"{', '.join(full)}. Tell the user which apps they are and to press "
                "Control + Command + F in each to leave full screen; don't retry.")
    return (f"{app_name} is running but hasn't shown a window in the background, so it can't "
            "be read yet. Tell the user and ask them to click its Dock icon once; don't retry.")


@function_tool
async def open_app(app_name: str) -> str:
    """Open a Mac app by its name, e.g. 'Music', 'Notes', 'Calculator'. It opens in the
    background so the user's current window stays in front."""
    if await _window_id(app_name):
        return f"{app_name} is open."
    # While any app is in full screen, a running app's window is on another desktop, and
    # "open" would switch the screen to it (even with -g), so don't.
    if await _is_running(app_name) and await asyncio.to_thread(_full_screen_apps):
        return _no_window(app_name)
    prev, opened = await asyncio.gather(_frontmost(), _bundle_id(app_name))
    proc = await asyncio.create_subprocess_exec("open", "-g", "-a", app_name)
    await proc.wait()
    if proc.returncode != 0:
        return f"Couldn't open {app_name}. Check the exact app name with list_open_apps."
    _give_focus_back(prev, opened)
    for i in range(20):
        if await _window_id(app_name):
            return f"{app_name} is open."
        if i == 8:
            # Apps like Slack hide their window when closed; ask them to show it again.
            await (await asyncio.create_subprocess_exec(
                "osascript", "-e", f'tell application "{app_name}" to reopen',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )).wait()
        await asyncio.sleep(0.15)
    return _no_window(app_name)


@function_tool
async def look_at_app(app_name: str, filter: str = "") -> str:
    """See the clickable controls in an app's window (and any pop-up sheet or alert on top).
    Returns lines like "e12 button 'Play'". Use the e-number ids with click/type_text.
    filter: optional text to only show controls whose label contains it (e.g. 'Radio').
    Labels are things like channel or contact names, so look without a filter when unsure."""
    d = await _ad("list-surfaces", "--app", app_name)
    surfaces = [s.get("type") for s in (d.get("data") or {}).get("surfaces", [])]
    for s in ("alert", "sheet"):
        if s in surfaces:
            snap = await _ad("snapshot", "--app", app_name, "--surface", s, "--compact")
            if snap.get("ok"):
                _ctx.get()["snap"] = snap["data"]["snapshot_id"]
                lines = _flatten(snap["data"]["tree"], False, filter)
                return f"A {s} pop-up is showing on top:\n" + "\n".join(lines[:150])
    win = await _window_id(app_name)
    args = ["--window-id", win] if win else ["--app", app_name]
    snap = await _ad("snapshot", *args, "-i", "--compact", "--max-depth", "25")
    if not snap.get("ok"):
        if (snap.get("error") or {}).get("code") == "WINDOW_NOT_FOUND":
            return _no_window(app_name)
        return _err(snap)
    _ctx.get()["snap"] = snap["data"]["snapshot_id"]
    lines = _flatten(snap["data"]["tree"], True, filter)
    extra = f"\n...and {len(lines) - 200} more (use filter)" if len(lines) > 200 else ""
    return "\n".join(lines[:200]) + extra if lines else "No matching controls found."


@function_tool
async def read_app_text(app_name: str, filter: str = "") -> str:
    """Read the visible text in an app's window (labels, values, results). Use it to check results."""
    win = await _window_id(app_name)
    args = ["--window-id", win] if win else ["--app", app_name]
    snap = await _ad(
        "snapshot", *args, "--compact", "--max-depth", "45", "--timeout-ms", "8000"
    )
    if not snap.get("ok"):
        if (snap.get("error") or {}).get("code") == "WINDOW_NOT_FOUND":
            return _no_window(app_name)
        return _err(snap)
    _ctx.get()["snap"] = snap["data"]["snapshot_id"]
    lines = _flatten(snap["data"]["tree"], False, filter)
    if len(lines) > 300:
        # Chat apps put the newest content at the bottom, so keep both ends.
        lines = lines[:80] + [f"... ({len(lines) - 300} lines skipped; use filter) ..."] + lines[-220:]
    return "\n".join(lines) or "No text found."


def _focus(take_focus: bool) -> tuple[str, ...]:
    return ("--headed",) if take_focus else ()


# Only one task at a time may bring an app forward and use the real keyboard/mouse.
_focus_lock = asyncio.Lock()


async def _ad_focus(take_focus: bool, *args: str) -> dict:
    if not take_focus:
        return await _ad(*args)
    async with _focus_lock:
        return await _ad(args[0], "--headed", *args[1:])


@function_tool
async def click(ref: str, take_focus: bool = False) -> str:
    """Click a control by its id from look_at_app, e.g. 'e12'. take_focus: see rule 6d."""
    d = await _ad_focus(take_focus, "click", _full_ref(ref))
    return "Clicked. Check the result." if d.get("ok") else _err(d)


@function_tool
async def type_text(ref: str, text: str, take_focus: bool = False) -> str:
    """Type text into a text field or document by its id from look_at_app. take_focus: see rule 6d."""
    d = await _ad_focus(take_focus, "type", _full_ref(ref), text)
    return "Typed. Check the result." if d.get("ok") else _err(d)


@function_tool
async def press_key(app_name: str, combo: str, take_focus: bool = False) -> str:
    """Press a key or shortcut in app_name without bringing it to the front,
    e.g. 'return', 'escape', 'cmd+n', 'cmd+l', 'space'. Use quit_app instead of cmd+q.
    take_focus: see rule 6d."""
    if combo.replace(" ", "").lower() in {"cmd+q", "command+q"}:
        return "Use quit_app to quit an app."
    d = await _ad_focus(take_focus, "press", "--app", app_name, combo)
    return "Pressed." if d.get("ok") else _err(d)


async def _is_running(app_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", f'application "{app_name}" is running',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() == "true"


@function_tool
async def quit_app(app_name: str) -> str:
    """Quit a Mac app by name (like choosing Quit from its menu). Checks that it actually closed."""
    if app_name.lower() in {"terminal", "finder"}:
        return f"I won't quit {app_name}; Mac Copilot needs it."
    if not await _is_running(app_name):
        return f"{app_name} isn't running."
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", f'tell application "{app_name}" to quit',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    for _ in range(16):
        if not await _is_running(app_name):
            return f"{app_name} has quit."
        await asyncio.sleep(0.5)
    return f"{app_name} is still open; it may be showing a 'save changes?' dialog. Look at it."


@function_tool
async def open_website(url: str, browser: str = "") -> str:
    """Open a website, e.g. url='youtube.com'. browser: the browser app the user named
    (e.g. 'Dia', 'Safari', 'Google Chrome'); leave empty for the default browser."""
    if "://" not in url:
        url = "https://" + url
    args = ["-g", "-a", browser, url] if browser else ["-g", url]
    prev = await _frontmost()
    opened = await _bundle_id(browser) if browser else _default_browser()
    proc = await asyncio.create_subprocess_exec(
        "open", *args, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    _give_focus_back(prev, opened)
    if proc.returncode != 0:
        return f"Couldn't open it: {err.decode().strip()[:200]}"
    return f"Opened {url}" + (f" in {browser}." if browser else ".")


@function_tool
async def list_installed_apps(filter: str = "") -> str:
    """List apps installed on the Mac (optionally only names containing filter)."""
    names: set[str] = set()
    for folder in ("/Applications", "/System/Applications", str(HOME / "Applications")):
        p = Path(folder)
        if p.exists():
            names |= {a.stem for a in p.glob("*.app")}
            names |= {a.stem for a in p.glob("*/*.app")}
    hits = sorted(n for n in names if filter.lower() in n.lower())
    return ", ".join(hits) or "No matching apps."


@function_tool
async def scroll(ref: str, direction: str = "down") -> str:
    """Scroll a scroll area (by id) up or down."""
    d = await _ad("scroll", _full_ref(ref), "--direction", direction, "--amount", "5")
    return "Scrolled." if d.get("ok") else _err(d)


@function_tool
async def set_dark_mode(on: bool) -> str:
    """Turn macOS Dark Mode on (true) or off (false)."""
    script = (
        'tell application "System Events" to tell appearance preferences '
        f"to set dark mode to {'true' if on else 'false'}"
    )
    proc = await asyncio.create_subprocess_exec("osascript", "-e", script)
    await proc.wait()
    return "Done." if proc.returncode == 0 else "Couldn't change Dark Mode (Automation permission?)."


@function_tool
async def set_volume(percent: int) -> str:
    """Set the Mac's output volume, 0-100."""
    percent = max(0, min(100, percent))
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", f"set volume output volume {percent}"
    )
    await proc.wait()
    return f"Volume set to {percent}%."


_KINDS = {
    "presentation": ["public.presentation"],
    "pdf": ["com.adobe.pdf"],
    "spreadsheet": ["public.spreadsheet"],
    "document": ["com.microsoft.word.doc", "org.openxmlformats.wordprocessingml.document",
                 "com.apple.iwork.pages.sffpages", "public.plain-text", "public.rtf"],
    "image": ["public.image"],
    "video": ["public.movie"],
    "folder": ["public.folder"],
}
_SKIP = ("/Library/", "/.", "/node_modules/", "/.venv/", "/site-packages/")


@function_tool
async def find_files(query: str = "", kind: str = "", days: int = 0) -> str:
    """Find files on the Mac with Spotlight, instantly and without opening anything.
    query: words in the file name or contents ("" for any); kind: presentation, pdf,
    spreadsheet, document, image, video or folder ("" for any); days: only files used or
    changed in the last N days (0 for any time; 1 = since yesterday). Newest first."""
    q = query.replace('"', "").replace("*", "").strip()
    parts = []
    if q:
        parts.append(f'(kMDItemDisplayName == "*{q}*"cd || kMDItemTextContent == "{q}"cd)')
    if kind:
        types = _KINDS.get(kind.lower())
        if not types:
            return f"kind must be one of: {', '.join(_KINDS)}."
        parts.append("(" + " || ".join(f'kMDItemContentTypeTree == "{t}"' for t in types) + ")")
    if days:
        parts.append(f"(kMDItemLastUsedDate >= $time.today(-{days}) || "
                     f"kMDItemFSContentChangeDate >= $time.today(-{days}))")
    if not parts:
        return "Say what to look for (words, a kind of file, or how recent)."
    proc = await asyncio.create_subprocess_exec(
        "mdfind", "-onlyin", str(Path.home()), " && ".join(parts),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        return "Spotlight took too long; try more specific words."
    paths = [p for p in out.decode().splitlines() if p and not any(x in p for x in _SKIP)]

    def stamp(p: str) -> float:
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0

    paths.sort(key=stamp, reverse=True)
    if not paths:
        return "No matching files."
    home = str(Path.home())
    lines = [f"- {p.replace(home, '~', 1)} (changed "
             f"{datetime.datetime.fromtimestamp(stamp(p)):%a %-d %b %H:%M})" for p in paths[:15]]
    return f"{len(paths)} found, newest first:\n" + "\n".join(lines)


@function_tool
async def open_file(path: str, app_name: str = "") -> str:
    """Open a file or folder (path from find_files, ~ allowed) in its usual app, or in
    app_name if given. It opens in the background; the user's window stays in front."""
    full = os.path.expanduser(path)
    if not os.path.exists(full):
        return "That file doesn't exist; use find_files to get the exact path."
    prev = await _frontmost()
    args = ["-g", "-a", app_name, full] if app_name else ["-g", full]
    proc = await asyncio.create_subprocess_exec("open", *args, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if app_name:
        _give_focus_back(prev, await _bundle_id(app_name))
    if proc.returncode:
        return f"Couldn't open it: {err.decode().strip()[:200]}"
    return f"Opened {path} in the background."


_REMINDER_SCRIPT = """
on run argv
    set t to item 1 of argv
    if (count of argv) > 1 then
        set d to current date
        set day of d to 1
        set year of d to (item 2 of argv) as integer
        set month of d to (item 3 of argv) as integer
        set day of d to (item 4 of argv) as integer
        set time of d to ((item 5 of argv) as integer) * 60
        tell application "Reminders" to make new reminder with properties {name:t, remind me date:d}
    else
        tell application "Reminders" to make new reminder with properties {name:t}
    end if
    return "ok"
end run
"""


@function_tool
async def add_reminder(title: str, day: str = "", time: str = "") -> str:
    """Add a reminder to Apple Reminders directly, without opening its window (works even when
    an app is in full screen). day: "today", "tomorrow" or YYYY-MM-DD; time: 24-hour HH:MM.
    Leave both empty for a reminder with no alert."""
    args = [title]
    if day or time:
        base = datetime.date.today()
        d = {"": base, "today": base, "tomorrow": base + datetime.timedelta(days=1)}.get(day.lower())
        try:
            d = d or datetime.date.fromisoformat(day)
            h, m = (int(x) for x in (time or "09:00").split(":"))
        except ValueError:
            return "Give day as today, tomorrow or YYYY-MM-DD and time as HH:MM."
        args += [str(d.year), str(d.month), str(d.day), str(h * 60 + m)]
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _REMINDER_SCRIPT, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        if b"-1743" in err:
            return ("macOS blocked it: allow Mac Copilot to control Reminders in System Settings → "
                    "Privacy & Security → Automation.")
        return f"Couldn't add it: {err.decode()[:200]}"
    when = f" for {d:%A %-d %B} at {h:02d}:{m:02d}" if len(args) > 1 else ""
    return f"Added reminder '{title}'{when}."


_LIST_REMINDERS_SCRIPT = """
tell application "Reminders" to set {ns, dds, rds} to {name, due date, remind me date} of (every reminder whose completed is false)
set now to current date
set out to {}
repeat with i from 1 to count of ns
    set d to item i of dds
    if d is missing value then set d to item i of rds
    if d is missing value then
        set end of out to "-" & tab & item i of ns
    else
        set end of out to ((d - now) as integer as text) & tab & item i of ns
    end if
end repeat
set AppleScript's text item delimiters to linefeed
return out as text
"""


@function_tool
async def list_reminders(day: str = "") -> str:
    """List open (not completed) Apple Reminders directly, without opening its window.
    day: "today", "tomorrow" or YYYY-MM-DD for reminders due that day (overdue ones are
    included for today), or "" for all open reminders."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _LIST_REMINDERS_SCRIPT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        if b"-1743" in err:
            return ("macOS blocked it: allow Mac Copilot to control Reminders in System Settings → "
                    "Privacy & Security → Automation.")
        return f"Couldn't read Reminders: {err.decode()[:200]}"
    now = datetime.datetime.now()
    items = []
    for line in out.decode().splitlines():
        off, _, name = line.partition("\t")
        items.append((None if off == "-" else now + datetime.timedelta(seconds=int(off)), name))
    if day:
        base = datetime.date.today()
        d = {"today": base, "tomorrow": base + datetime.timedelta(days=1)}.get(day.lower())
        try:
            d = d or datetime.date.fromisoformat(day)
        except ValueError:
            return "Give day as today, tomorrow or YYYY-MM-DD."
        items = [(w, n) for w, n in items
                 if w and (w.date() == d or (d == base and w.date() < base))]
    if not items:
        return f"No open reminders{' for ' + day if day else ''}."
    items.sort(key=lambda x: (x[0] is None, x[0] or now))
    lines = [f"- {n} ({w:%a %-d %b %H:%M})" if w else f"- {n} (no date)" for w, n in items[:40]]
    more = f"\n…and {len(items) - 40} more" if len(items) > 40 else ""
    return f"{len(items)} open reminder(s):\n" + "\n".join(lines) + more


from calendar_tools import CALENDAR_TOOLS  # noqa: E402
from messages_tools import MESSAGE_TOOLS  # noqa: E402
from screen_tools import SCREEN_TOOLS  # noqa: E402
from slack_tools import SLACK_TOOLS  # noqa: E402
from gmail_tools import EMAIL_TOOLS  # noqa: E402
from agent_tools import AGENT_TOOLS  # noqa: E402

ALL_TOOLS = [
    list_open_apps, open_app, look_at_app, read_app_text, click, type_text,
    press_key, scroll, set_dark_mode, set_volume, quit_app, open_website,
    list_installed_apps, add_reminder, list_reminders, find_files, open_file, *CALENDAR_TOOLS, *MESSAGE_TOOLS, *SCREEN_TOOLS,
    *SLACK_TOOLS, *EMAIL_TOOLS, *AGENT_TOOLS,
]

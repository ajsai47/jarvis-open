"""Phone tools: read the spare iPhone in the background through WebDriverAgent (Apple's UI-testing bridge).

No window, mouse or keyboard on the Mac is used. Setup lives in .env:
PHONE_UDID (the phone), PHONE_TEAM (Xcode team id), PHONE_WDA_BUNDLE, and optionally PHONE_WDA_DIR.
Milestone 1 is read-only: tools only open apps, navigate to known screens and read.
"""

import asyncio
import datetime
import re
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from agents import function_tool

CACHE = Path.home() / "Library/Caches/phoneuse"
APPS = {"settings": "com.apple.Preferences", "uber": "com.ubercab.UberClient", "uber eats": "com.ubercab.UberEats",
        "instagram": "com.burbn.instagram", "venmo": "net.kortina.labs.Venmo", "messages": "com.apple.MobileSMS",
        "photos": "com.apple.mobileslideshow", "calendar": "com.apple.mobilecal", "maps": "com.apple.Maps"}
KEEP = {"Button", "Cell", "StaticText", "Switch", "TextField", "SearchField", "Link", "Tab", "NavigationBar"}
_lock = threading.Lock()  # held by the worker thread, so a cancelled request can't release it early
_abort = threading.Event()  # set when the caller gave up (tool timeout): the worker thread must not tap after that
_sid = ""


class PhoneError(Exception):
    pass


_base = ""


def _tunnel_url() -> str:
    """WDA via Apple's CoreDevice tunnel to the phone: works over the cable or Wi-Fi, no iproxy needed."""
    udid = os.environ.get("PHONE_UDID", "")
    if not udid:
        return ""
    try:
        out = subprocess.run(["xcrun", "devicectl", "device", "info", "details", "--device", udid],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""
    m = re.search(r"Tunnel IP Address:\s*([0-9a-fA-F:.]+)", out)
    if not m:
        return ""
    ip = m[1]
    return f"http://[{ip}]:8100" if ":" in ip else f"http://{ip}:8100"


def _wda(method: str, path: str, body=None, timeout: float = 20):
    url = os.environ.get("PHONE_WDA_URL") or _base or "http://127.0.0.1:8100"
    req = urllib.request.Request(url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        data = e.read()
        msg = ""
        try:
            msg = json.loads(data)["value"].get("message", "")
        except Exception:
            pass
        raise PhoneError(msg or f"HTTP {e.code}") from None
    return json.loads(data)


def _up() -> bool:
    global _base
    try:
        _wda("GET", "/status", timeout=3)
        return True
    except Exception:
        pass
    if os.environ.get("PHONE_WDA_URL"):
        return False
    for url in ("http://127.0.0.1:8100", _tunnel_url()):  # cable forward first, then the tunnel (Wi-Fi)
        if url and url != _base:
            _base = url
            try:
                _wda("GET", "/status", timeout=3)
                return True
            except Exception:
                pass
    _base = ""
    return False


def _start() -> None:
    """Start the cable tunnel and the WebDriverAgent runner, then wait until it answers."""
    udid, team = os.environ.get("PHONE_UDID", ""), os.environ.get("PHONE_TEAM", "")
    bundle = os.environ.get("PHONE_WDA_BUNDLE", "")
    wda_dir = Path(os.environ.get("PHONE_WDA_DIR", CACHE / "wda"))
    if not (udid and team and bundle and wda_dir.exists()):
        raise PhoneError("The phone isn't set up for Jarvis yet (PHONE_UDID / PHONE_TEAM / PHONE_WDA_BUNDLE in .env).")
    if subprocess.run(["pgrep", "-f", "iproxy 8100"], capture_output=True).returncode:
        subprocess.Popen(["/opt/homebrew/bin/iproxy", "8100", "8100", "-u", udid], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    subprocess.run(["pkill", "-f", "xcodebuild -project WebDriverAgent"], capture_output=True)
    log = open(CACHE / "wda.log", "w")
    subprocess.Popen(["xcodebuild", "-project", "WebDriverAgent.xcodeproj", "-scheme", "WebDriverAgentRunner",
                      "-destination", f"id={udid}", "-allowProvisioningUpdates", f"DEVELOPMENT_TEAM={team}",
                      "CODE_SIGN_STYLE=Automatic", f"PRODUCT_BUNDLE_IDENTIFIER={bundle}", "test"],
                     cwd=wda_dir, stdout=log, stderr=log, start_new_session=True)
    for _ in range(60):
        time.sleep(2)
        if _up():
            return
    raise PhoneError("The phone helper didn't start. Is the spare unlocked, trusted, and plugged in or on the Mac's Wi-Fi?")


def _session(fresh: bool = False) -> str:
    global _sid
    if not _up():
        _start()
    if _wda("GET", "/wda/locked").get("value"):
        raise PhoneError("The spare phone is locked. Tell the user to unlock it (Jarvis never types the passcode).")
    if fresh or not _sid:
        _sid = _wda("POST", "/session", {"capabilities": {"alwaysMatch": {}}})["sessionId"]
        _wda("POST", f"/session/{_sid}/appium/settings",
             {"settings": {"useFirstMatch": True, "animationCoolOffTimeout": 0, "waitForIdleTimeout": 0}})
    return _sid


def _run(fn):
    """Run a phone action; restart the helper once if the phone revoked its UI-testing permission, and start a new
    session if the helper restarted since the last one."""
    try:
        return fn(_session())
    except PhoneError as e:
        if "Not authorized" in str(e) or "invalid session" in str(e).lower():
            _start()
            return fn(_session(fresh=True))
        if "session does not exist" in str(e).lower():
            return fn(_session(fresh=True))
        raise


def _tap_at(sid: str, x: float, y: float) -> None:
    if _abort.is_set():
        raise PhoneError("Stopped: the request timed out, so nothing more is tapped.")
    _wda("POST", f"/session/{sid}/actions", {"actions": [{
        "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
        "actions": [{"type": "pointerMove", "duration": 0, "x": int(x), "y": int(y)},
                    {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 80},
                    {"type": "pointerUp", "button": 0}]}]})


def _find(sid: str, pred: str, timeout: float = 5.0) -> str:
    t0 = time.time()
    while True:
        try:
            v = _wda("POST", f"/session/{sid}/element", {"using": "predicate string", "value": pred})["value"]
            return v.get("ELEMENT") or v.get("element-6066-11e4-a52e-4f735466cecf")
        except PhoneError as e:
            if "Not authorized" in str(e) or "session" in str(e).lower():
                raise
            if time.time() - t0 > timeout:
                raise PhoneError(f"Couldn't find {pred} on the phone.") from None
            time.sleep(0.15)


def _click(sid: str, eid: str) -> None:
    if _abort.is_set():
        raise PhoneError("Stopped: the request timed out, so nothing more is tapped.")
    _wda("POST", f"/session/{sid}/element/{eid}/click")


def _launch(sid: str, app: str) -> None:
    bundle = APPS.get(app.strip().lower(), app.strip())
    _wda("POST", f"/session/{sid}/wda/apps/launch", {"bundleId": bundle})


def _elements(sid: str, filt: str = "") -> list[str]:
    xml = _wda("GET", f"/session/{sid}/source?format=xml&excluded_attributes=accessible,enabled,index",
               timeout=30)["value"]
    out, seen = [], set()
    for n in ET.fromstring(xml).iter():
        t = n.tag.replace("XCUIElementType", "")
        lab = (n.get("label") or n.get("name") or "").strip()
        if t not in KEEP or not lab or n.get("visible") == "false" or lab in seen or (lab.isupper() and t != "Button") or lab == "chevron":
            continue
        seen.add(lab)
        val = n.get("value")
        line = f"{t}: {lab}" + (f" = {val}" if t == "Switch" and val is not None else "")
        if not filt or filt.lower() in line.lower():
            out.append(line[:140])
    return out


def _locked_run(fn):
    # Give up instead of queueing: a request whose caller already timed out must not drive the phone later.
    if not _lock.acquire(timeout=10):
        raise PhoneError("The phone is busy with another request; try again in a moment.")
    _abort.clear()
    try:
        return _run(fn)
    finally:
        _lock.release()


async def _go(fn) -> str:
    try:
        return await asyncio.to_thread(_locked_run, fn)
    except asyncio.CancelledError:
        _abort.set()  # asyncio can't stop the thread, so stop its next tap
        raise
    except PhoneError as e:
        return f"Phone: {e}"
    except Exception as e:
        return f"Phone error: {str(e)[:200]}"


@function_tool
async def phone_status() -> str:
    """Is the spare iPhone ready for phone tasks (connected, helper running, unlocked)? Starts the helper if needed."""
    def f(sid):
        app = _wda("GET", "/wda/activeAppInfo").get("value", {}).get("bundleId", "?")
        return f"The spare phone is ready (unlocked). Front app: {app}."
    return await _go(f)


@function_tool
async def phone_look(app: str = "", filter: str = "") -> str:
    """Read what's on the spare iPhone's screen, in the background (nothing on the Mac moves). app: optional app
    to open first ("Uber", "Instagram", "Settings", "Venmo", or a bundle id). filter: only lines containing it.
    Returns the screen's buttons, text and switches, top to bottom. Read-only."""
    def f(sid):
        if app:
            _launch(sid, app)
            time.sleep(3)
        els = _elements(sid, filter)
        return "\n".join(els[:80]) or "Nothing readable on screen."
    return await _go(f)


@function_tool
async def phone_screen_time(day: str = "yesterday") -> str:
    """The spare iPhone's Screen Time total for a day: "yesterday", "today", or a date like "November 8".
    Read-only, runs in the background (~7 s)."""
    if day.strip().lower() == "yesterday":
        label = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%B %d")
    elif day.strip().lower() == "today":
        label = datetime.date.today().strftime("%B %d")
    else:
        try:
            d = datetime.datetime.strptime(day.strip().title(), "%B %d")
            label = d.strftime("%B %d")
        except ValueError:
            return 'day must be "yesterday", "today" or like "November 8".'
    def f(sid):
        _wda("POST", "/wda/homescreen")
        _wda("POST", f"/session/{sid}/wda/apps/terminate", {"bundleId": APPS["settings"]})
        _launch(sid, "settings")
        field = _find(sid, "type == 'XCUIElementTypeSearchField'")
        _click(sid, field)
        _wda("POST", f"/session/{sid}/element/{field}/value", {"value": list("Screen Time"), "text": "Screen Time"})
        _click(sid, _find(sid, "label == 'Screen Time' AND type == 'XCUIElementTypeButton'"))
        _click(sid, _find(sid, "label == 'See All App & Website Activity'"))
        el = _find(sid, f"label BEGINSWITH '{label}'")
        bar = _wda("GET", f"/session/{sid}/element/{el}/attribute/label")["value"]
        top = [l for l in _elements(sid) if l.startswith("Button:") and "," in l][:4]
        return f"Screen Time on the spare, {bar}." + (" Top apps this week: " + "; ".join(t[8:] for t in top) if top else "")
    return await _go(f)


@function_tool
async def phone_uber_status() -> str:
    """Check Uber on the spare iPhone for a ride in progress (driver, ETA). Read-only, background."""
    def f(sid):
        _launch(sid, "uber")
        time.sleep(3)
        els = _elements(sid)
        text = " ".join(els).lower()
        if any(k in text for k in ("arriving", "your driver", "is on the way", "min away", "meet at", "trip")):
            return "Uber shows a trip:\n" + "\n".join(els[:25])
        if "where to?" in text:
            return "No Uber ride in progress (Uber is on its 'Where to?' screen)."
        return "Uber screen:\n" + "\n".join(els[:25])
    return await _go(f)


GATE = re.compile(r"\b(send|request|pay|post|share|delete|remove|buy|order|book|ride|reserve|confirm|call|"
                  r"facetime|follow|reply|erase|reset|sign out|log out|turn on|turn off|allow|accept|install|"
                  r"subscribe|checkout|place|submit|transfer|cash out)\b", re.I)


def _match(sid: str, label: str) -> str:
    q = label.replace('"', '\\"')
    return _find(sid, f'label == "{q}" OR name == "{q}"', 3.0) if label else ""


@function_tool
async def phone_tap(label: str, approved: bool = False) -> str:
    """Tap a button, row or field on the spare iPhone by its exact label from phone_look (e.g. "Where to?").
    Taps that send, pay, request, book, post, delete, confirm, etc. are blocked unless approved=True, which you
    may only set after showing the user exactly what will happen and getting their yes."""
    if GATE.search(label) and not approved:
        return (f"Blocked: tapping '{label}' could send, pay or change something. Tell the user exactly what it "
                "will do and ask; only after their yes call phone_tap again with approved=True.")
    def f(sid):
        _click(sid, _match(sid, label))
        time.sleep(1.0)
        return f"Tapped '{label}'. Screen now:\n" + "\n".join(_elements(sid)[:40])
    return await _go(f)


@function_tool
async def phone_type(text: str, field: str = "") -> str:
    """Type text into a field on the spare iPhone (replacing what's there). field: the field's label from
    phone_look; empty means the first text/search field on screen. Typing never sends anything by itself."""
    def f(sid):
        el = _match(sid, field) if field else _find(sid, "type == 'XCUIElementTypeSearchField' OR type == 'XCUIElementTypeTextField'", 3.0)
        _click(sid, el)
        note = ""
        try:
            _wda("POST", f"/session/{sid}/element/{el}/clear")
        except PhoneError:
            note = " (the field couldn't be cleared first, so older text may still be in it)"
        _wda("POST", f"/session/{sid}/element/{el}/value", {"value": list(text), "text": text})
        time.sleep(1.2)
        return f"Typed '{text}'{note}. Screen now:\n" + "\n".join(_elements(sid)[:40])
    return await _go(f)


@function_tool
async def phone_back() -> str:
    """Go back one screen on the spare iPhone by tapping its Back button. Many apps title the back button with the
    previous screen's name (e.g. "Settings"); if this says there's no Back button, use phone_look and phone_tap
    the top-left button by its label instead."""
    def f(sid):
        try:
            btn = _find(sid, "type == 'XCUIElementTypeButton' AND (name == 'BackButton' OR label == 'Back' OR name == 'Back')", 1.0)
        except PhoneError as e:
            if not str(e).startswith("Couldn't find"):
                raise
            raise PhoneError("No button called Back here; phone_look and tap the top-left button by its label.") from None
        _click(sid, btn)
        time.sleep(1.0)
        return "Tapped Back. Screen now:\n" + "\n".join(_elements(sid)[:40])
    return await _go(f)


@function_tool
async def phone_home() -> str:
    """Go to the spare iPhone's Home screen."""
    return await _go(lambda sid: (_wda("POST", "/wda/homescreen"), "On the Home screen.")[1])


LINKEDIN = "com.linkedin.LinkedIn"
LI_LOG = Path(__file__).parent / "logs" / "linkedin_sent.json"
LI_DAILY_CAP, LI_GAP_S = 20, 30  # one at a time, human pace: LinkedIn restricts accounts that look automated


def _li_dismiss(sid: str) -> None:
    """Close LinkedIn's payment banner: it covers the message box and tapping it opens Campaign Manager billing."""
    try:
        _find(sid, 'type == "XCUIElementTypeLink" AND label CONTAINS "update your payment method"', 1.0)
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Close"', 1.0))
        time.sleep(1.0)
    except PhoneError as e:
        if not str(e).startswith("Couldn't find"):
            raise


def _li_field(sid: str, pred: str, text: str) -> str:
    el = _find(sid, pred, 5.0)
    _click(sid, el)
    try:
        _wda("POST", f"/session/{sid}/element/{el}/clear")
    except PhoneError:
        pass
    _wda("POST", f"/session/{sid}/element/{el}/value", {"value": list(text), "text": text})
    time.sleep(1.5)
    return el


def _li_send(name: str, message: str):
    """Fresh LinkedIn launch, open the 1:1 thread with exactly `name`, type, check, Send, check it posted."""
    def f(sid):
        _wda("POST", f"/session/{sid}/wda/apps/terminate", {"bundleId": LINKEDIN})
        _launch(sid, LINKEDIN)
        time.sleep(4)
        _li_dismiss(sid)
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Messaging"', 8.0))
        time.sleep(2.5)
        _li_field(sid, 'type == "XCUIElementTypeSearchField" AND label == "Search messages"', name)
        time.sleep(1.5)
        q = name.replace('"', '\\"')
        rows = _wda("POST", f"/session/{sid}/elements",
                    {"using": "predicate string", "value": f'type == "XCUIElementTypeCell" AND label BEGINSWITH[c] "{q},"'})["value"]
        found = {}
        for r in rows:
            eid = r.get("ELEMENT") or r.get("element-6066-11e4-a52e-4f735466cecf")
            if _wda("GET", f"/session/{sid}/element/{eid}/attribute/visible").get("value") in (True, "true"):
                found.setdefault(_wda("GET", f"/session/{sid}/element/{eid}/attribute/label").get("value"), eid)
        rows = list(found.values())  # the same thread can be listed twice (e.g. hidden copy); count distinct rows
        if not rows:
            raise PhoneError(f"No 1:1 LinkedIn conversation with {name}. Jarvis only messages people you already have a thread with.")
        if len(rows) > 1:
            raise PhoneError(f"{len(rows)} LinkedIn conversations start with {name}; ask which one (use their full name).")
        _click(sid, rows[0])
        time.sleep(3)
        _find(sid, 'type == "XCUIElementTypeNavigationBar" AND name CONTAINS "ConversationView"', 5.0)
        _find(sid, f'type == "XCUIElementTypeStaticText" AND label ==[c] "{q} button"', 3.0)
        _li_dismiss(sid)
        _li_field(sid, 'type == "XCUIElementTypeTextView"', message)  # the thread's only text box; may hold an old draft
        box = _find(sid, 'type == "XCUIElementTypeTextView"', 3.0)  # re-find: the box is a new element once it has text
        typed = _wda("GET", f"/session/{sid}/element/{box}/attribute/value").get("value") or ""
        if typed.strip() != message.strip():
            raise PhoneError(f"The message box shows {typed!r}, not the approved text, so nothing was sent.")
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Send Message"', 3.0))
        time.sleep(2.5)
        m = message.strip().replace('"', '\\"')
        _find(sid, f'type == "XCUIElementTypeButton" AND label == "{m}"', 5.0)
        return f"Sent on LinkedIn to {name}: {message}"
    return f


@function_tool
async def linkedin_message(name: str, message: str) -> str:
    """Send a LinkedIn message as the user from the spare iPhone, to someone he already has a 1:1 thread with
    (their full name as LinkedIn shows it). Only after the user has confirmed the exact recipient and wording.
    One person per call; never bulk or cold outreach."""
    import decisions
    if why := decisions.gate("linkedin", name, message):
        return why
    log = json.loads(LI_LOG.read_text()) if LI_LOG.exists() else []
    now, today = time.time(), datetime.date.today().isoformat()
    if sum(1 for x in log if x.get("day") == today) >= LI_DAILY_CAP:
        return f"Not sent: {LI_DAILY_CAP} LinkedIn messages already today (cap keeps the account safe)."
    if log and now - log[-1].get("at", 0) < LI_GAP_S:
        await asyncio.sleep(LI_GAP_S - (now - log[-1]["at"]))
    r = await _go(_li_send(name, message))
    if r.startswith("Sent"):
        LI_LOG.write_text(json.dumps((log + [{"at": time.time(), "day": today, "to": name}])[-200:]))
    decisions.sent("linkedin", name, message, r)
    return r



LI_CONNECT_CAP = 15  # requests a day; LinkedIn's weekly invitation limit is about 100
_DEG = re.compile(r" • (1st|2nd|3rd\+)")


_LI_TAB = 'type == "XCUIElementTypeButton" AND label == "{}" AND rect.y > 800'


def _li_screen(sid: str) -> str:
    """Which LinkedIn screen is up, from an accessibility snapshot."""
    xml = _wda("GET", f"/session/{sid}/source?format=xml&excluded_attributes=accessible,enabled,index", timeout=30)["value"]
    root = ET.fromstring(xml)

    def has(kind: str, label: str | None = None, top: bool | None = None) -> bool:
        for n in root.iter(f"XCUIElementType{kind}"):
            y = int(n.get("y") or 0)
            if n.get("visible") == "false" or (label is not None and n.get("label") != label):
                continue
            if top is None or (y < 140) == top:
                return True
        return False

    for a in root.iter("XCUIElementTypeAlert"):
        if a.get("visible") != "false":
            return f"alert: {a.get('label') or 'a system alert'}"  # iOS or LinkedIn pop-up; never answered for the user
    if has("Other", "dismiss popup"):
        return "sheet"  # a bottom sheet (e.g. a profile's More menu); tapping outside closes it
    if has("Button", "Close menu"):
        return "menu"  # the side menu from Menu (your own profile, pages, settings)
    if has("Button", "Back button") and has("Button", "More"):
        return "profile"  # someone's profile: Follow/Connect, Message, More
    if has("TextField", top=True) and has("Button", "Clear text", top=True) or has("TextField", top=True) and has("Key"):
        return "suggestions"  # search box with the keyboard up
    if has("StaticText", "Search", top=True):
        return "results"  # search results: the bar at the top is text, not a box
    if has("Button", "Search", top=True):
        return "home"
    if has("Button", "Home", top=False):
        return "tab"  # Jobs/Video, or Home scrolled so its top bar is hidden
    if has("Button", "Back"):
        return "page"
    return "unknown"


def _li_open_search(sid: str) -> None:
    """Get to LinkedIn's search box without restarting the app, else stop.

    Back walks back through every earlier search, so leave search screens with the tab bar, not Back."""
    state = _wda("POST", f"/session/{sid}/wda/apps/state", {"bundleId": LINKEDIN}).get("value")
    if state != 4:  # 4 = running in front; 1 = not running
        _wda("POST", f"/session/{sid}/wda/apps/activate", {"bundleId": LINKEDIN})
        time.sleep(4 if state in (0, 1) else 2)
    _li_dismiss(sid)
    where = last = ""
    for _ in range(6):
        last, where = where, _li_screen(sid)
        if where == "suggestions":
            return
        if where == last == "results":  # the bar's tap didn't land (e.g. an iOS banner over it): use the tab bar
            where = "tab"
        tap = {"menu": 'type == "XCUIElementTypeButton" AND label == "Close menu"',
               "sheet": 'type == "XCUIElementTypeOther" AND label == "dismiss popup"',
               "profile": 'type == "XCUIElementTypeButton" AND label == "Back button"',
               "results": 'type == "XCUIElementTypeStaticText" AND label == "Search" AND rect.y < 140',
               "home": 'type == "XCUIElementTypeButton" AND label == "Search" AND rect.y < 140',
               # Home hides its top bar once the feed scrolls, and tapping Home again only refreshes it;
               # the Network tab always shows the bar
               "tab": _LI_TAB.format("Network"),
               "page": 'type == "XCUIElementTypeButton" AND label == "Back"'}.get(where)
        if not tap:
            break
        _click(sid, _find(sid, tap, 2.0))
        time.sleep(2.0)
    if where.startswith("alert: "):
        raise PhoneError(f"A pop-up is covering LinkedIn ({where[7:]}); stopped. Ask the user to answer it on the spare.")
    raise PhoneError(f"Couldn't get to LinkedIn's search box (stuck on a {where or 'unknown'} screen); "
                     "stopped without restarting the app.")


def _li_people(sid: str, name: str) -> list[dict]:
    """People search for `name`, staying in LinkedIn. Read-only: search results don't count as profile views."""
    _li_open_search(sid)
    for _ in range(2):  # the box can keep the last search; clear it and check what's typed
        box = _find(sid, 'type IN {"XCUIElementTypeSearchField","XCUIElementTypeTextField"}', 5.0)
        try:
            _wda("POST", f"/session/{sid}/element/{box}/clear")
        except PhoneError:
            pass
        _wda("POST", f"/session/{sid}/element/{box}/value", {"value": list(name), "text": name})
        time.sleep(1.5)
        box = _find(sid, 'type IN {"XCUIElementTypeSearchField","XCUIElementTypeTextField"}', 3.0)
        typed = (_wda("GET", f"/session/{sid}/element/{box}/attribute/value").get("value") or "").strip()
        if typed == name.strip():
            break
    else:
        raise PhoneError(f"LinkedIn's search box shows {typed!r}, not {name!r}.")
    chip = 'type == "XCUIElementTypeButton" AND label IN {"Filter by People","People"}'
    # Return sometimes leaves the suggestions list up with the keyboard gone: tap the key, or refocus the box and retry
    for submit in ("key", "show all", "refocus", "key"):
        if submit == "refocus":
            try:
                _click(sid, _find(sid, 'type IN {"XCUIElementTypeSearchField","XCUIElementTypeTextField"}', 2.0))
                time.sleep(1.0)
            except PhoneError:
                pass
        if submit == "refocus":
            _wda("POST", f"/session/{sid}/wda/keys", {"value": ["\n"]})
        else:
            label = '"Show all results"' if submit == "show all" else '{"search","return"}'
            try:
                _click(sid, _find(sid, f'type == "XCUIElementTypeButton" AND label IN[c] {label}'.replace('IN[c] "', '==[c] "'), 1.5))
            except PhoneError:
                continue
        try:
            eid = _find(sid, chip, 6.0)
            break
        except PhoneError as e:
            if not str(e).startswith("Couldn't find"):
                raise
    else:
        raise PhoneError(f"LinkedIn didn't show search results for {name}.")
    if _wda("GET", f"/session/{sid}/element/{eid}/attribute/label").get("value") == "Filter by People":
        _click(sid, eid)
    first = name.split()[0].lower()
    for i in range(5):  # results can take a few seconds to load
        time.sleep(2.5)
        people = _li_rows(sid)
        if any(r["label"].lower().startswith(first) for r in people):
            break
        if i < 4 and any(e.endswith("Remove all filters") for e in _elements(sid)):
            # a filter left over from an earlier list (e.g. someone's mutual connections) hides everyone: clear it
            _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Remove all filters"', 2.0))
            time.sleep(2.5)
            try:
                _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Filter by People"', 4.0))
            except PhoneError:
                pass
    return people


def _li_rows(sid: str) -> list[dict]:
    xml = _wda("GET", f"/session/{sid}/source?format=xml&excluded_attributes=accessible,enabled,index", timeout=30)["value"]
    buttons = [n for n in ET.fromstring(xml).iter("XCUIElementTypeButton") if n.get("visible") != "false"]
    people, acts = [], []
    for n in buttons:
        lab, y, h = n.get("label") or "", int(n.get("y") or 0), int(n.get("height") or 0)
        lab = lab if lab.startswith(("Invite ", "Pending, click")) else " ".join(lab.split())  # LinkedIn mixes in no-break spaces
        if m := _DEG.search(lab):
            people.append({"label": lab, "degree": m[1], "y": y, "h": h, "act": ""})
        elif lab.startswith(("Invite ", "Pending, click to withdraw", "Follow ")):
            acts.append((y, lab))
    for y, lab in acts:  # an Invite/Pending/Follow button sits beside its result row
        for r in people:
            if r["y"] <= y < r["y"] + r["h"] and not r["act"]:
                r["act"] = lab
    return people


def _li_pick(people: list[dict], name: str, company: str) -> tuple[dict | None, str]:
    """The one search result that is `name` (at `company` when given), or None and why."""
    n = " ".join(name.split()).lower()
    same = [r for r in people if r["label"].lower().startswith(n) and r["label"][len(n):len(n) + 1] in (" ", ",")]
    if not same:
        return None, f"no one called {name} in LinkedIn search"
    if company.strip():
        at = [r for r in same if company.strip().lower() in r["label"].lower()]
        if len(at) == 1:
            return at[0], ""
        if not at:
            return None, f"{len(same)} {name} in search, none mentioning {company}; check by hand"
        return None, f"{len(at)} people called {name} at {company}; check by hand"
    if len(same) == 1:
        return same[0], ""
    return None, f"{len(same)} people called {name}; add their company"


def _li_state(r: dict) -> str:
    if r["degree"] == "1st":
        return "connected"
    if r["act"].startswith("Pending"):
        return "pending"
    if r["act"].startswith("Invite "):
        return "can connect"
    if r["act"].startswith("Follow "):
        return "follow only"  # Follow-first profile: Connect is under More on their profile
    return "no Connect button"


def _li_find(sid: str, name: str, company: str) -> tuple[dict | None, str]:
    r, why = _li_pick(_li_people(sid, name), name, company)
    if r is None and company.strip() and "none mentioning" in why:
        # headline doesn't name the company: search "name company" and take the one exact-name result
        r, why2 = _li_pick(_li_people(sid, f"{name} {company}"), name, "")
        if r is None:
            why += f"; searching with {company} found {why2.split(';')[0]}"
    return r, why


def _li_lookup(name: str, company: str = ""):
    def f(sid):
        r, why = _li_find(sid, name, company)
        return ("not sure: " + why) if r is None else _li_state(r)
    return f


def _li_connect(name: str, company: str = ""):
    """Search, pick exactly this person, tap Invite (never adds a note), check it now says Pending."""
    def f(sid):
        r, why = _li_find(sid, name, company)
        if r is None:
            raise PhoneError(f"Not sent: {why}.")
        st = _li_state(r)
        if st == "follow only":
            return _li_connect_profile(sid, r, name)
        if st != "can connect":
            return f"Nothing to do: {name} is {st}."
        shown = r["act"][len("Invite "):-len(" to connect")]
        act = r["act"].replace('"', '\\"')
        _click(sid, _find(sid, f'type == "XCUIElementTypeButton" AND label == "{act}"', 3.0))
        _li_after_invite(sid, name)
        q = shown.replace('"', '\\"')
        _find(sid, f'type == "XCUIElementTypeButton" AND label == "Pending, click to withdraw invitation sent to {q}"', 5.0)
        return f"Connection request sent to {name} (no note)."
    return f


def _li_after_invite(sid: str, name: str) -> None:
    """After tapping Connect: skip the note offer ("Send without a note"), stop on limits or an email prompt."""
    time.sleep(2.5)
    try:
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label BEGINSWITH "Send without"', 1.5))
        time.sleep(2)
    except PhoneError as e:
        if not str(e).startswith("Couldn't find"):
            raise
    page = " ".join(_elements(sid)).lower()
    if "invitation limit" in page or "weekly limit" in page:
        raise PhoneError("LinkedIn says you've hit the invitation limit; stopping.")
    if "email" in page and "pending" not in page:
        raise PhoneError(f"LinkedIn wants {name}'s email address to connect; nothing was sent.")


def _li_connect_profile(sid: str, r: dict, name: str) -> str:
    """Follow-first people: open their profile from the result row (a profile view), More -> Connect, no note."""
    row = None
    for e in _wda("POST", f"/session/{sid}/elements", {"using": "predicate string",
                                                       "value": 'type == "XCUIElementTypeButton"'})["value"]:
        eid = e.get("ELEMENT") or e.get("element-6066-11e4-a52e-4f735466cecf")
        rect = _wda("GET", f"/session/{sid}/element/{eid}/rect")["value"]
        if int(rect["y"]) == r["y"] and int(rect["height"]) == r["h"]:
            row = rect
            break
    if row is None:
        raise PhoneError(f"Not sent: lost {name}'s search result.")
    # The row's middle is often its "N mutual connections" link, which opens a list instead: tap the name line.
    _tap_at(sid, row["x"] + min(120, row["width"] / 2), row["y"] + 24)
    time.sleep(3)
    if _li_screen(sid) != "profile":
        raise PhoneError(f"Not sent: tapping {name}'s result didn't open a profile.")
    xml = _wda("GET", f"/session/{sid}/source?format=xml&excluded_attributes=accessible,enabled,index", timeout=30)["value"]
    if not any(" ".join((n.get("label") or "").split()) == name and n.get("visible") != "false"
               for n in ET.fromstring(xml).iter()):  # the profile's name heading is an Other element
        raise PhoneError(f"Not sent: the profile that opened isn't {name}.")
    _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "More"', 3.0))
    time.sleep(2)
    try:  # "Invite <name> to connect, Connect"; never "Personalize invite" (that's a note)
        eid = _find(sid, 'type == "XCUIElementTypeButton" AND label BEGINSWITH "Invite " '
                         'AND label ENDSWITH " to connect, Connect"', 2.0)
    except PhoneError:
        page = " ".join(_elements(sid)).lower()
        _li_leave_profile(sid)
        if "pending" in page or "withdraw" in page:
            return f"Nothing to do: {name} is pending."
        raise PhoneError(f"Not sent: {name}'s More menu has no Connect.") from None
    lab = " ".join(_wda("GET", f"/session/{sid}/element/{eid}/attribute/label")["value"].split())
    if lab != f"Invite {name} to connect, Connect":
        _li_leave_profile(sid)
        raise PhoneError(f"Not sent: the menu says {lab!r}, not {name}.")
    _click(sid, eid)
    _li_after_invite(sid, name)
    ok = "pending" in " ".join(_elements(sid)).lower()
    if not ok:  # the profile's own buttons may not say it; the More menu does
        if _li_screen(sid) == "sheet":
            _click(sid, _find(sid, 'type == "XCUIElementTypeOther" AND label == "dismiss popup"', 2.0))
            time.sleep(1.5)
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "More"', 3.0))
        time.sleep(2)
        page = " ".join(_elements(sid)).lower()
        ok = ("pending" in page or "withdraw" in page) and f"invite {name.lower()} to connect" not in page
    _li_leave_profile(sid)
    if not ok:
        raise PhoneError(f"Tapped Connect for {name} but LinkedIn doesn't show Pending; check the spare.")
    return f"Connection request sent to {name} (no note, from their profile)."


def _li_leave_profile(sid: str) -> None:
    if _li_screen(sid) == "sheet":
        _click(sid, _find(sid, 'type == "XCUIElementTypeOther" AND label == "dismiss popup"', 2.0))
        time.sleep(1.5)
    if _li_screen(sid) == "profile":
        _click(sid, _find(sid, 'type == "XCUIElementTypeButton" AND label == "Back button"', 2.0))
        time.sleep(2)


def _li_log() -> list:
    return json.loads(LI_LOG.read_text()) if LI_LOG.exists() else []


def _li_room(kind: str) -> int:
    today = datetime.date.today().isoformat()
    cap = LI_CONNECT_CAP if kind == "connect" else LI_DAILY_CAP
    return cap - sum(1 for x in _li_log() if x.get("day") == today and x.get("kind", "message") == kind)


async def _li_pace(gap: float) -> None:
    log = _li_log()
    if log and (wait := gap - (time.time() - log[-1].get("at", 0))) > 0:
        await asyncio.sleep(wait)


def _li_note(kind: str, name: str) -> None:
    LI_LOG.write_text(json.dumps((_li_log() + [{"at": time.time(), "day": datetime.date.today().isoformat(),
                                                "to": name, "kind": kind}])[-500:]))


@function_tool
async def linkedin_connect(name: str, company: str = "") -> str:
    """Send a LinkedIn connection request, with no note, from the spare iPhone. name: as LinkedIn shows it;
    company: where they work (used to pick the right person). Only after the user said yes to this person.
    Already connected or pending: does nothing. Follow-first people: opens their profile (they see the view)."""
    import decisions
    what = f"Connection request (no note) to {name}" + (f" at {company}" if company else "")
    if why := decisions.gate("linkedin", name, what):
        return why
    if _li_room("connect") <= 0:
        return f"Not sent: {LI_CONNECT_CAP} connection requests already today (cap keeps the account safe)."
    await _li_pace(LI_GAP_S)
    r = await _go(_li_connect(name, company))
    if r.startswith("Connection request sent"):
        _li_note("connect", name)
    decisions.sent("linkedin", name, what, r)
    return r


def _sheet_rows(file: str, day: str, limit: int):
    import linkedin_sheet
    path, rows = linkedin_sheet.read(file)
    if day.strip():
        rows = [r for r in rows if r.get("send_day", "").split(".")[0] == day.strip()]
    return path, rows[:max(1, min(limit, 60))]


@function_tool
async def linkedin_check(file: str, day: str = "", limit: int = 20) -> str:
    """Read-only: for people in a LinkedIn list (.xlsx/.csv with name and company columns; a path or a file name in
    Downloads/Desktop/Documents), find whether the user is already connected, pending, can connect, or follow only (Connect only on their
    profile). Uses LinkedIn search on the spare iPhone (no profile views). Writes a linkedin_status column into the file. day: only rows with
    this send_day. About 20 s per person."""
    import linkedin_sheet
    try:
        path, rows = _sheet_rows(file, day, limit)
    except Exception as e:
        return f"Couldn't read {file}: {e}"
    out, upd = [], {}
    for r in rows:
        st = await _go(_li_lookup(r["name"], r.get("company", "")))
        if st.startswith("Phone"):
            out.append(f"{r['name']}: {st}")
            break
        upd[r["_row"]] = {"linkedin_status": st}
        out.append(f"{r['name']} ({r.get('company', '')}): {st}")
    linkedin_sheet.write(path, upd)
    return f"Checked {len(upd)} of {len(rows)} in {path.name} (saved to its linkedin_status column):\n" + "\n".join(out)


@function_tool
async def linkedin_batch(file: str, action: str = "connect", day: str = "", limit: int = 5) -> str:
    """Work through a LinkedIn list (.xlsx/.csv) from the spare iPhone, one person at a time at a human pace
    (1-3 minutes apart), writing what happened into its `sent` column. action "connect": connection requests with
    no note. action "message": sends each row's message (or followup_dm) column, only to people with an existing
    1:1 thread. Skips rows already marked sent, and (connect) rows whose linkedin_status says connected, pending or
    needs a check by hand. day: only rows with this send_day. limit: at most this many
    (default 5). The first call shows the user the whole batch and waits for one yes; call it again unchanged
    after the yes. Stops at the first problem."""
    import random
    import decisions
    import linkedin_sheet
    action = "message" if action.strip().lower().startswith("mess") else "connect"
    try:
        path, rows = _sheet_rows(file, day, 10_000)
    except Exception as e:
        return f"Couldn't read {file}: {e}"
    rows = [r for r in rows if not r.get("sent")]
    if action == "connect":  # after linkedin_check: skip connected/pending and rows that need a check by hand
        rows = [r for r in rows if (r.get("linkedin_status") or "").strip().lower() in ("", "can connect", "follow only")]
    if action == "message":
        rows = [r for r in rows if r.get("message") or r.get("followup_dm")]
    rows = rows[:max(1, min(limit, 20))]
    if not rows:
        return f"No unsent rows to {action} in {path.name}" + (f" for day {day}." if day else ".")
    if action == "connect":
        plan = "\n".join(f"{i}. {r['name']}" + (f" ({r['company']})" if r.get("company") else "") for i, r in enumerate(rows, 1))
        what = f"LinkedIn connection requests, no note, from {path.name}:\n{plan}"
    else:
        plan = "\n".join(f"{i}. {r['name']}: {r.get('message') or r['followup_dm']}" for i, r in enumerate(rows, 1))
        what = f"LinkedIn messages from {path.name}:\n{plan}"
    if why := decisions.gate("linkedin", f"{len(rows)} people", what):
        return why
    done, out = 0, []
    for i, r in enumerate(rows):
        if _li_room(action) <= 0:
            out.append(f"Stopped: daily {action} cap reached.")
            break
        if i:
            await asyncio.sleep(random.uniform(60, 180))
        if action == "connect":
            res = await _go(_li_connect(r["name"], r.get("company", "")))
            ok = res.startswith("Connection request sent")
        else:
            res = await _go(_li_send(r["name"], r.get("message") or r["followup_dm"]))
            ok = res.startswith("Sent")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        if ok:
            _li_note(action, r["name"])
            done += 1
            val = f"{'requested' if action == 'connect' else 'messaged'} {stamp}"
        elif res.startswith("Nothing to do"):
            val = res.split(" is ", 1)[-1].rstrip(".")
        else:
            val = ""
        if val:
            linkedin_sheet.write(path, {r["_row"]: {"sent": val}})
        out.append(f"{r['name']}: {res}")
        if not ok and not res.startswith("Nothing to do"):
            if "Not sent:" not in res and "No 1:1" not in res:  # a skipped row is fine; anything else stops
                out.append("Stopped here so nothing goes out wrong.")
                break
    decisions.sent("linkedin", f"{len(rows)} people", what, "\n".join(out))
    return f"{done} of {len(rows)} done ({path.name} updated):\n" + "\n".join(out)


# Slower than helpers.TOOL_TIMEOUT (60 s): a search takes ~20-45 s and a batch waits 1-3 min between people.
linkedin_message.timeout_seconds = 240
linkedin_connect.timeout_seconds = 300
linkedin_check.timeout_seconds = 60 * 60
linkedin_batch.timeout_seconds = 2 * 60 * 60

PHONE_WRITE = [phone_tap, phone_type, phone_back, phone_home, linkedin_message, linkedin_connect, linkedin_batch]


PHONE_TOOLS = [phone_status, phone_look, phone_screen_time, phone_uber_status, linkedin_check]

PHONE_RULE = """
Phone: you can read the user's spare iPhone in the background with phone_status, phone_look, phone_screen_time
and phone_uber_status (nothing on the Mac moves). Use them for phone-only things: Screen Time, Uber/Lyft status,
apps with no Mac or web version. With phone_tap/phone_type/phone_back/phone_home you can operate apps (fill searches, open screens). Taps that
send, pay, request, book or post are blocked until the user says yes: show exactly what will happen, ask, then
retry with approved=True. If a phone tool says the phone is locked or not set up, tell the user exactly that.
LinkedIn messages: always linkedin_message (it finds the 1:1 thread, checks the name and the typed text, and holds the
send for the user's yes); never phone_tap/phone_type through LinkedIn Messaging yourself.
LinkedIn connections: linkedin_connect (one person, never a note); linkedin_check (read-only: who in a list is already
connected / pending); linkedin_batch (a list file, one yes for the whole batch, human pace). Never tap Connect yourself.
For a list file, call linkedin_check / linkedin_batch straight away: they read the file and linkedin_batch shows
the real batch for the yes. Never say who is in a file without a tool having read it.
Helpers have no phone tools: do phone steps yourself, never hand them to run_helpers/run_read_helpers.
"""

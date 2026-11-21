"""Page watches: "tell me when this drops under $200". Jarvis re-reads the page in the cloud (TinyFish
Fetch, free) on a schedule; only when the page text has changed does a quick model check the condition.
When it's met, a check-in card drops down in the notch and the user gets a text on his phone (logs/watches.json)."""
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from agents import Agent, RunConfig, Runner, function_tool
from pydantic import BaseModel

import cloud_web as cw
import settings

FILE = Path(__file__).resolve().parent / "logs" / "watches.json"
MAX_WATCHES = 20
MIN_EVERY, MAX_EVERY = 15, 7 * 24 * 60
MAX_DAYS = 30
_lock = threading.RLock()


def load() -> list[dict]:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []


def save(ws: list[dict]) -> None:
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ws))
    tmp.replace(FILE)


def minutes(every: str) -> int | None:
    """'30 minutes', 'hourly', 'every 2 hours', 'daily' -> minutes."""
    w = every.strip().lower().removeprefix("every").strip()
    named = {"hourly": 60, "hour": 60, "an hour": 60, "daily": 1440, "day": 1440, "a day": 1440}
    if w in named:
        return named[w]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", w)
    if not m:
        return None
    return round(float(m[1]) * {"m": 1, "h": 60, "d": 1440}[m[2][0]])


def add(url: str, condition: str, every: str = "1 hour", text_me: bool = True) -> str:
    n = minutes(every or "1 hour")
    if n is None:
        return "I couldn't read how often. Say e.g. '30 minutes', 'every 2 hours' or 'daily'."
    if not re.match(r"https?://", url.strip()):
        return "I need the page's full address (https://...)."
    n = max(MIN_EVERY, min(MAX_EVERY, n))
    with _lock:
        ws = load()
        if sum(w["state"] == "watching" for w in ws) >= MAX_WATCHES:
            return f"You already have {MAX_WATCHES} watches. Stop one first."
        wid = max((w["id"] for w in ws), default=0) + 1
        ws.append({"id": wid, "url": url.strip(), "condition": condition.strip(), "every": n, "text_me": text_me,
                   "state": "watching", "created": time.time(), "next": 0, "hash": "", "last": "", "checks": 0})
        save(ws)
    return f"Watch {wid} set: I'll check {url.strip()} every {_every(n)} and tell you when {condition.strip()}."


def _every(n: int) -> str:
    return f"{n} minutes" if n < 60 else f"{n // 60} hour{'s' * (n >= 120)}" if n < 1440 else f"{n // 1440} day{'s' * (n >= 2880)}"


def stop(wid: int) -> bool:
    return _update(wid, state="stopped")


def _update(wid: int, **kw) -> bool:
    with _lock:
        ws = load()
        hit = [w for w in ws if w["id"] == wid]
        for w in hit:
            w.update(kw)
        save(ws)
        return bool(hit)


def answer(wid: int, verdict: str) -> str:
    """The Keep / Snooze / Drop card for a watch that fired."""
    if verdict == "snooze":
        _update(wid, state="watching", next=time.time() + 3600, hash="")
        return "OK, I'll keep watching and tell you again if it's still true in an hour."
    _update(wid, state="done")
    return "Got it. I've stopped watching that page." if verdict == "keep" else "Dropped. I've stopped watching it."


class Verdict(BaseModel):
    met: bool
    detail: str


JUDGE = Agent(name="Page watch", instructions=(
    "You check whether a condition is true on a web page. You get the condition and the page's text. "
    "met: true only if the page clearly shows it is true right now. detail: one short sentence with the "
    "fact that matters, e.g. 'AirPods Pro 3 are $189 at Best Buy (was $249)'. If the page doesn't show it, "
    "met is false and detail says what it shows instead."), output_type=Verdict)


async def _judge(condition: str, title: str, text: str) -> Verdict:
    JUDGE.model = settings.agent_model("quick")
    r = await Runner.run(JUDGE, f"Condition: {condition}\n\nPage: {title}\n\n{text[:cw.PAGE_CHARS]}",
                         run_config=RunConfig(tracing_disabled=settings.is_local("quick")))
    return r.final_output


async def check_due(now: float | None = None) -> list[tuple[dict, str]]:
    """Checks every watch that's due. Returns [(watch, detail)] for the ones whose condition is now met."""
    import asyncio
    now = now or time.time()
    fired = []
    for w in [w for w in load() if w["state"] == "watching" and w["next"] <= now]:
        if now - w["created"] > MAX_DAYS * 86400:
            _update(w["id"], state="expired")
            continue
        upd = {"next": now + w["every"] * 60, "checks": w["checks"] + 1, "checked": now}
        try:
            r = await asyncio.to_thread(cw._call, cw.FETCH_URL, {"urls": [w["url"]]}, 60)
            page = (r.get("results") or [{}])[0]
            text = page.get("text") or ""
            if not text:
                upd["last"] = "Couldn't read the page: " + str((r.get("errors") or [{}])[0].get("error", "empty"))
            else:
                h = hashlib.sha256(text.encode()).hexdigest()
                if h != w["hash"]:
                    v = await _judge(w["condition"], page.get("title") or "", text)
                    upd.update(hash=h, last=v.detail)
                    if v.met:
                        upd["state"] = "fired"
                        fired.append((w, v.detail))
        except Exception as e:
            upd["last"] = f"Check failed: {str(e)[:150]}"
        _update(w["id"], **upd)
    return fired


@function_tool
async def watch_page(url: str, condition: str, every: str = "1 hour", text_me: bool = True) -> str:
    """Watch a public web page and tell the user when a condition becomes true, e.g. "the price drops under
    $200" or "it's back in stock". Checks run in the cloud (free); nothing opens on the Mac. url: the page's full
    address (find it with search_web first if needed). every: how often, e.g. "30 minutes", "1 hour", "daily"
    (at least 15 minutes). text_me: also text the user's phone when it happens."""
    return add(url, condition, every, text_me)


@function_tool
async def list_watches() -> str:
    """The pages Jarvis is watching, with their id, condition and what the last check found."""
    ws = [w for w in load() if w["state"] in ("watching", "fired")]
    if not ws:
        return "You aren't watching any pages."
    return "\n".join(f"{w['id']}. {w['url']}: tell you when {w['condition']}, every {_every(w['every'])}"
                     f" ({'fired' if w['state'] == 'fired' else str(w['checks']) + ' checks'}). Last: {w['last'] or 'not checked yet'}"
                     for w in ws)


@function_tool
async def stop_watching(watch_id: int) -> str:
    """Stop watching a page (id from list_watches)."""
    return "Stopped." if stop(watch_id) else "No watch with that id."


WATCH_TOOLS = [watch_page, list_watches, stop_watching]
WATCH_READ = {"list_watches"}

"""Jev router: handle simple one-step requests instantly, send everything else to gpt-5.5.

Jev (TypeSafe's decision model) picks the route; code does the action. Anything Jev is
unsure about, or that might send/delete/buy/post, goes to the LLM as before.
"""

import asyncio
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agents.tool_context import ToolContext

from desktop_tools import HOME, open_app, open_website, quit_app, set_dark_mode, set_volume

ROUTES = {
    "volume": "Only change the Mac's sound volume (louder, quieter, mute, a percentage).",
    "dark_on": "Only turn Dark Mode on.",
    "dark_off": "Only turn Dark Mode off / switch to Light Mode.",
    "open_app": "Only open or launch one app. Nothing else happens inside the app.",
    "quit_app": "Only quit or close one app. Nothing else.",
    "website": "Only open one website or web page, optionally in a named browser. Nothing else.",
    "llm": "Anything else: several steps, clicking or typing inside an app, playing specific "
           "music, reading, summarizing, searching, writing, questions, or anything unclear.",
}
SITES = {
    "youtube": "youtube.com", "spotify": "open.spotify.com", "tiktok": "tiktok.com",
    "twitter": "x.com", "x": "x.com", "gmail": "mail.google.com", "google": "google.com",
    "netflix": "netflix.com", "linkedin": "linkedin.com", "reddit": "reddit.com",
    "github": "github.com", "amazon": "amazon.com", "instagram": "instagram.com",
}
BROWSERS = ["Dia", "Safari", "Google Chrome", "Arc", "Firefox", "Brave Browser"]
KINDS = {
    "read": "Only reading, checking or looking things up: questions, summaries, searching, "
            "seeing what's new or waiting. Nothing is changed, typed, created or sent.",
    "work": "Changing something that's easy to undo: typing, drafting, filling in, creating a note, "
            "reminder or event, clicking through an app, playing music, changing a setting.",
    "send": "Sending, posting, replying, submitting, deleting, buying or anything that can't be undone.",
}
MIN_CONFIDENCE = 0.8
# Opening things is harmless and easy to undo, so it needs less certainty.
LOW_RISK = {"open_app": 0.6, "website": 0.6}
MAX_SENSITIVE = 0.3


_TELL = re.compile(r"Tell the user(?: which apps they are)?(?: and(?: ask them)?)? to (\w)(.*?); don't retry\.")


def _for_user(msg: str) -> str:
    """Tool results speak to the model ("Tell the user to ..."); Jev shows them to the user directly."""
    return _TELL.sub(lambda m: m.group(1).upper() + m.group(2) + ".", str(msg))


@dataclass
class Routed:
    handled: bool
    route: str
    confidence: float
    ms: int
    reply: str = ""
    kind: str = "work"  # read: only looks things up; work: changes something; send: can't be undone
    app: str = ""       # the app window a work/send task needs; "" = no window (tools/APIs only), "*" = unclear


def _installed_apps() -> list[str]:
    names: set[str] = {"Finder"}
    for folder in ("/Applications", "/System/Applications", str(HOME / "Applications")):
        p = Path(folder)
        if p.exists():
            names |= {a.stem for a in p.glob("*.app")} | {a.stem for a in p.glob("*/*.app")}
    return sorted(names)[:250]


APPS = _installed_apps()


def _ask_jev(request: str) -> dict:
    questions = {
        "route": {"type": "choice", "instructions":
                  "A user asked their Mac assistant this. Which handler fits the whole request?",
                  "criteria": ROUTES},
        "question": {"type": "noul", "instructions":
                     "Is the request a question asking for information (e.g. 'is Slack open?', "
                     "'what song is this?') rather than a command to do something?"},
        "sensitive": {"type": "noul", "instructions":
                      "Would doing this request send a message or email, post publicly, delete "
                      "something, buy something, or change passwords or security settings?"},
        "kind": {"type": "choice", "instructions":
                 "What will doing this request involve?",
                 "criteria": KINDS},
        "target": {"type": "choice", "instructions":
                   "Which app's window will doing this request need to click or type in?",
                   "criteria": {"tools": "No app window: Slack, email, texts, calendar, reminders, files, "
                                         "AI agents, settings, or just opening an app or website.",
                                "unclear": "Unclear, or several apps.",
                                **{f"a{i}": name for i, name in enumerate(APPS)}}},
        "app": {"type": "choice", "instructions":
                "Which installed app does the request name as the app to open or quit?",
                "criteria": {"none": "No app, or the app is not in this list.",
                             **{f"a{i}": name for i, name in enumerate(APPS)}}},
    }
    body = json.dumps({"model": "jev-latest", "state": {"request": request},
                       "questions": questions}).encode()
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["answers"]


def _volume(text: str) -> str | None:
    if re.search(r"\bmute\b", text, re.I):
        return "0"
    m = re.search(r"\b(\d{1,3})\b", text)
    return m.group(1) if m and int(m.group(1)) <= 100 else None


def _website(text: str) -> tuple[str, str] | None:
    browser = next((b for b in BROWSERS if re.search(rf"\b{re.escape(b)}\b", text, re.I)), "")
    m = re.search(r"\b[\w-]+(\.[\w-]+)*\.(com|org|net|io|ai|tv|co|app|dev)(/\S*)?", text, re.I)
    if m:
        return m.group(0), browser
    for word, url in SITES.items():
        if re.search(rf"\b{word}\b", text, re.I):
            return url, browser
    return None


def _classify(a: dict) -> tuple[str, str]:
    """Jev's answers -> (kind, app). Unsure answers lean to the safer side: work over read,
    send if it might be sensitive, and "*" (wait for every other app task) if the app is unclear."""
    kind = a.get("kind") or {}
    k = kind.get("choice", "work")
    if a["sensitive"]["noul"] > 0.5 or k == "send":
        k = "send"
    elif k == "read" and kind.get("confidence", 0) < 0.7:
        k = "work"
    t = a.get("target") or {}
    choice = t.get("choice", "unclear")
    if choice == "tools" and t.get("confidence", 0) >= 0.6:
        app = ""
    elif choice.startswith("a") and choice[1:].isdigit() and t.get("confidence", 0) >= 0.6:
        app = APPS[int(choice[1:])]
    else:
        app = "*"
    return k, app


async def _call(tool, **args) -> str:
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="router",
                      tool_arguments=json.dumps(args))
    return str(await tool.on_invoke_tool(ctx, json.dumps(args)))


async def route_request(text: str, execute: bool = True) -> Routed:
    """Try to handle text directly. handled=False means: give it to the LLM."""
    t0 = time.time()
    if not os.environ.get("TYPESAFE_API_KEY"):
        return Routed(False, "llm", 0.0, 0, app="*")
    try:
        a = await asyncio.to_thread(_ask_jev, text)
    except Exception:
        return Routed(False, "llm", 0.0, int((time.time() - t0) * 1000), app="*")
    route, conf = a["route"]["choice"], a["route"]["confidence"]
    ms = int((time.time() - t0) * 1000)
    kind, app = _classify(a)
    llm = Routed(False, route, conf, ms, kind=kind, app=app)
    if (route == "llm" or conf < LOW_RISK.get(route, MIN_CONFIDENCE)
            or a["sensitive"]["noul"] > MAX_SENSITIVE or a["question"]["noul"] > 0.5):
        return llm

    app = a["app"]
    no_app = app["choice"] == "none" or app["confidence"] < MIN_CONFIDENCE
    if route == "open_app" and no_app and _website(text):
        route = "website"  # e.g. "open TikTok" when TikTok is a website, not an app

    if route in ("open_app", "quit_app"):
        if no_app:
            return llm
        name = APPS[int(app["choice"][1:])]
        call, args = (open_app if route == "open_app" else quit_app), {"app_name": name}
    elif route == "volume":
        level = _volume(text)
        if level is None:
            return llm
        call, args = set_volume, {"percent": int(level)}
    elif route in ("dark_on", "dark_off"):
        call, args = set_dark_mode, {"on": route == "dark_on"}
    elif route == "website":
        site = _website(text)
        if not site:
            return llm
        call, args = open_website, {"url": site[0], "browser": site[1]}
    else:
        return llm

    if not execute:
        return Routed(True, f"{call.name} {json.dumps(args)}", conf, ms, kind="work", app="")
    reply = _for_user(await _call(call, **args))
    return Routed(True, call.name, conf, int((time.time() - t0) * 1000), reply, kind="work", app="")

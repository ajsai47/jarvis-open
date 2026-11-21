"""Mac Copilot, web mode: a Jarvis-style page at http://localhost:8765 that runs the same
Jev -> gpt-5.5 -> agent-desktop brain as the Terminal modes.

Push-to-talk: hold the page's mic button, or Option + Command (on the page even while voice
is off, which turns it on until it's unused for 2 minutes; in any app while voice is on). The voice side
(copilot_voice.py, gpt-realtime-2.1) runs inside this server and streams its events to the page.

Run: .venv/bin/python copilot_web.py         (--mock tries the page without touching the Mac,
                                              --no-voice starts with the microphone off)
"""

import base64
import argparse
import asyncio
import contextlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn

import checkins
import decisions
import people
import rules
import settings
from media_hush import HUSH
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse

PORT = 8765
WEB = Path(__file__).resolve().parent / "web"
STATE = Path(__file__).resolve().parent / "logs" / "state.json"
HERE = Path(__file__).resolve().parent
CONVERSATIONS = STATE.parent / "conversations"  # one file per past conversation (History)
LOCAL_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
TAILNET = ""  # this Mac's Tailscale name: the page may also be opened from the user’s own devices via `tailscale serve`

MOCK = False
START_VOICE = False
MAX_TALK_S = 60
AUTO_OFF_S = 120
AGENT = None
READ_AGENT = None
history: list = []  # finished exchanges, shared by every new task


def _trim(h: list) -> None:
    """Keep the newest exchanges that fit the memory settings (user + assistant = 2 items)."""
    del h[:-2 * settings.get("memory_turns")]
    budget, keep = settings.get("memory_chars"), 0
    for item in reversed(h):
        budget -= len(str(item.get("content", "")))
        if budget < 0:
            break
        keep += 1
    keep -= keep % 2
    del h[:len(h) - keep]
MAX_TASKS = 200

# Live events for the page (voice turns, listening, speaking), separate from typed-command streams.
subscribers: set[asyncio.Queue] = set()
voice: dict = {"want": False, "state": "off", "task": None, "mod": None, "vc": None,
               "release": None, "auto": False, "last": 0.0}


def publish(**event) -> None:
    if event.get("type") == "voice" and event.get("state") != "warn":
        voice["state"] = event["state"]
    if event.get("src") == "voice":
        voice["last"] = time.monotonic()
        if event.get("type") == "speaking" and not MOCK:
            with suppress(Exception):
                HUSH.speaking(bool(event.get("on")))
    with suppress(Exception):
        _track(event)
    for q in list(subscribers):
        q.put_nowait(event)


# ---- live preview: the app windows running tasks are working in. Pictures are taken only when
# the page asks (Preview tab open and visible), shown to the user and never saved or sent to a model.
_surfaces: dict[str, dict] = {}
_task_ctx: dict[str, dict] = {}  # task id -> its desktop_tools context (holds its Dia tab)


def _track(event: dict) -> None:
    typ, tid = event.get("type"), event.get("tid") or event.get("src") or ""
    if typ in ("end", "stopped", "error"):
        for k in [k for k, v in _surfaces.items() if v["tid"] == tid]:
            _surfaces.pop(k)
        _task_ctx.pop(str(tid), None)
        return
    if typ == "helper" and event.get("state") in ("done", "error"):
        _surfaces.pop(f"{tid}:{event.get('app', '')}", None)
        return
    if not (typ == "tool" or (typ == "helper" and event.get("state") == "tool")):
        return
    name = event.get("name", "")
    try:
        args = json.loads(event.get("args") or "{}")
    except (TypeError, ValueError):
        args = {}
    app = "Dia" if name.startswith("web_") else (isinstance(args, dict) and args.get("app_name")) or event.get("app", "")
    if app:
        _surfaces[f"{tid}:{app}"] = {"key": f"{tid}:{app}", "tid": tid, "app": app, "tool": name,
                                     "args": str(event.get("args", ""))[:300], "web": name.startswith("web_"),
                                     "at": time.time()}


def _voice_event(**event) -> None:
    if event.get("type") in ("heard", "said") and event.get("text"):
        (decisions.heard if event["type"] == "heard" else decisions.said)(str(event["text"]))
    publish(src="voice", **event)


# ---- tasks: typed requests run side by side. Jev classes each one (read / work / send) and
# names the app window it needs; only tasks that need the same window wait for each other.

SENT_CLAIM = re.compile(r"\b(?:sent|submitted|requested|invited|connected with|messaged|emailed|texted)\b", re.I)
PROMISE = re.compile(r"(?:(?:ok(?:ay)?|sure|got it)[,.!]?\s*)?(?:i can check|i['’]ll|i will|let me|checking)\b", re.I)
CONFIRM = re.compile(r"\b(send|post|delete|submit|buy|publish|reply|go ahead|start|launch)\b[^?\n]{0,40}\?\s*$", re.I)
YES = re.compile(r"^\s*(yes|yep|yeah|sure|ok(ay)?|go ahead|send it|do it|approved?|confirm(ed)?)\b", re.I)
NO = re.compile(r"^\s*(no|nope|cancel|don'?t)\b", re.I)


class Task:
    def __init__(self, tid: int, text: str) -> None:
        self.id, self.text = tid, text
        self.state, self.kind, self.app, self.via = "working", "", "", ""
        self.reply, self.waiting = "", ""
        self.history: list | None = None  # this task's own transcript, for follow-ups ("yes")
        self.task: asyncio.Task | None = None
        self.log = None
        self.auto = ""  # set when Jarvis started it by itself (the daily briefing)
        self.plan = self.step = self.of = 0  # step 2 of 5 in plan 7 (several requests sent at once)
        self.after: Task | None = None  # the step that has to finish first
        self.said = ""  # the whole message, kept on step 1
        self.decision: dict | None = None  # the user’s latest answer to this task's draft (for the decision log)
        self.retried = False
        self.draft: dict | None = None  # the exact message the draft is about (show_draft), editable on the card
        self.run = ""  # id of the latest run in logs/runs.jsonl
        self.checkin = ""  # set on a reminder / check-in card: what it's about ("rem:3", "mc:grok:12")
        self.at = time.time()
        self.acts: list = []  # [tool, args, helper app] for the Plan panel, so a reload still shows them
        self.brief = False  # asked from the phone: the reply is read aloud, so keep it short
        self.front = ""  # asked from the notch: the window the user was in (screen_tools.front_note)

    def emit(self, **event) -> None:
        if event.get("type") == "tool" or (event.get("type") == "helper" and event.get("state") == "tool"):
            self.acts = [*self.acts[-59:], [event.get("name", ""), str(event.get("args", ""))[:300], event.get("app", "")]]
        publish(src="task", tid=self.id, **event)

    def set(self, state: str, waiting: str = "") -> None:
        self.state, self.waiting = state, waiting
        self.emit(type="state", state=state, waiting=waiting)

    def info(self) -> dict:
        return {"tid": self.id, "text": self.text, "state": self.state, "kind": self.kind,
                "app": self.app, "via": self.via, "reply": self.reply, "waiting": self.waiting, "auto": self.auto,
                "plan": self.plan, "step": self.step, "of": self.of, "said": self.said, "at": self.at, "draft": self.draft,
                "checkin": self.checkin, "acts": self.acts}


tasks: dict[int, Task] = {}
_seq = 0
_running: dict[int, str] = {}  # task id -> app window it is using ("*" = unclear, so any)
_waiting: dict[int, str] = {}  # task id -> app window it is waiting for, in arrival order
_sched: asyncio.Condition | None = None
_ends: asyncio.Condition | None = None  # wakes plan steps waiting for the step before


def _clash(a: str, b: str) -> bool:
    return a == b or "*" in (a, b)


def _free(tid: int, key: str) -> bool:
    if any(_clash(key, k) for k in _running.values()):
        return False
    for w, k in _waiting.items():  # first come, first served
        if w == tid:
            return True
        if _clash(key, k):
            return False
    return True


async def _acquire(t: Task, key: str) -> None:
    global _sched
    if not key:
        return
    _sched = _sched or asyncio.Condition()
    async with _sched:
        _waiting[t.id] = key
        try:
            if not _free(t.id, key):
                busy = next((k for k in _running.values() if _clash(key, k)), "*")
                t.set("queued", "other tasks" if "*" in (key, busy) else key)
                await _sched.wait_for(lambda: _free(t.id, key))
            _running[t.id] = key
        finally:
            _waiting.pop(t.id, None)
            _sched.notify_all()


async def _release(t: Task) -> None:
    if _sched and t.id in _running:
        async with _sched:
            _running.pop(t.id, None)
            _sched.notify_all()


def _finish(t: Task, text: str, reply: str, ask: bool) -> None:
    t.reply = reply
    t.state = "needs_ok" if ask else "done"
    t.emit(type="reply", text=reply, ask=ask)
    if not ask:  # a draft still waiting for an OK must never reach other tasks, or their "yes" could send it
        history.extend([{"role": "user", "content": t.text}, {"role": "assistant", "content": reply}])
        _trim(history)


async def _route(t: Task, text: str) -> bool:
    """Jev: do it instantly (True), or class it for gpt-5.5."""
    from router import route_request
    from runlog import RunLog
    t.log = RunLog("web", text)
    quick = await route_request(text)
    t.via = "jev" if quick.handled else settings.label("quick" if quick.kind == "read" else "work")
    t.kind, t.app = ("work", "") if quick.handled else (quick.kind, quick.app)
    t.log.route(t.via, quick.ms, quick.route if quick.handled else None)
    t.emit(type="route", via=t.via, ms=quick.ms, route=quick.route, kind=t.kind, app=t.app)
    if quick.handled:
        _finish(t, text, quick.reply, False)
        t.log.finish("done", quick.reply)
    return quick.handled


async def _work(t: Task, text: str) -> None:
    from agents import RunConfig, Runner
    from agents.items import ToolCallItem
    from runlog import RunLog
    import helpers

    log = t.log or RunLog("web", text)
    t.log = None
    read = t.kind == "read"
    flag: dict = {}
    helpers.EMIT.set(t.emit)
    helpers.NEEDS_MORE.set(flag if read else None)
    start = t.history if t.history is not None else list(history)
    t.run = log.run.get("id", "")
    decisions.CTX.set({**(t.decision or {"request": t.text}), "run": t.run})
    box: dict = {}
    decisions.BOX.set(box)
    agent, use = (READ_AGENT, "quick") if read else (AGENT, "work")
    agent.model = settings.agent_model(use)
    local, used_tool = settings.is_local(use), False
    ask = helpers.stamp(helpers.demo_note(text)) + t.front + (PHONE_NOTE if t.brief else "") + (LOCAL_NOTE if local and not read else "")
    t.front = ""
    result = Runner.run_streamed(agent, start + [{"role": "user", "content": ask}], max_turns=40,
                                 run_config=RunConfig(tracing_disabled=local))
    try:
        async for ev in result.stream_events():
            if ev.type == "run_item_stream_event":
                log.item(ev.item)
                if isinstance(ev.item, ToolCallItem):
                    used_tool = True
                    raw = ev.item.raw_item
                    t.emit(type="tool", name=getattr(raw, "name", "tool"), args=getattr(raw, "arguments", ""))
    except asyncio.CancelledError:
        result.cancel()
        log.finish("stopped")
        raise
    except Exception as e:
        log.finish("error", f"{type(e).__name__}: {str(e)[:300]}")
        if local and not used_tool:  # the model on this Mac failed before doing anything: redo it in the cloud
            settings.ollama_down()
            t.via = settings.label(use)
            t.emit(type="route", via=t.via, ms=0, route=None, kind=t.kind, app=t.app)
            return await _work(t, text)
        raise
    if read and flag.get("reason"):
        # Jev guessed read-only but it needs more: run again with every tool.
        log.finish("handed over", flag["reason"])
        t.kind = "work"
        t.emit(type="escalate", text=flag["reason"], kind="work")
        await _acquire(t, _lock_key(t))
        t.set("working")
        return await _work(t, text)
    reply = str(result.final_output)
    if read and not used_tool and not t.retried and PROMISE.match(reply.strip()) and len(reply) < 300:
        # It said it would look but answered before looking: once more, told to look now.
        t.retried = True
        log.finish("retried", reply)
        return await _work(t, text + "\n\n(Use your tools now and answer with what they show.)")
    if not read and not used_tool and SENT_CLAIM.search(reply):
        # It said it sent something without calling any tool (copying an earlier send in the chat): nothing went out.
        if not t.retried:
            t.retried = True
            log.finish("retried", reply)
            return await _work(t, text + "\n\n(Nothing has been sent yet: call the send tool now; don't say it's sent unless the tool says so.)")
        reply = "I didn't send anything: I said it was sent without actually doing it. Please ask again."
    with contextlib.suppress(Exception):
        log.run["usage"] = settings.usage(settings.model(use), result.context_wrapper.usage)
    t.history = result.to_input_list()
    t.draft = dict(box) or None
    _finish(t, text, reply, not read and bool(CONFIRM.search(reply)))
    log.finish("done", reply)


LOCAL_NOTE = ("\n\n(To draft a text, email, Slack or agent message, call show_draft, or the send tool itself: "
              "without the user’s yes it only shows him the draft card. Then ask \"Send it?\".)")
PHONE_NOTE = ("\n\n(Asked from the user’s phone, and the reply is read aloud. Answer in 1-3 short spoken sentences with "
              "just the key facts: no tables, lists or markdown. Offer more detail if there's more worth knowing. "
              "Check what was asked about fresh with the one matching tool (earlier answers in the chat may be out of date), "
              "and don't check other sources the question isn't about.)")


def _lock_key(t: Task) -> str:
    """"" = needs no window. Website tasks each get their own Dia tab when the extension is on."""
    from browser_bridge import BRIDGE, BROWSERS
    if t.kind == "read" or (t.app in BROWSERS and BRIDGE.connected):
        return ""
    return t.app


# ---- plans: several requests in one message (one per line, or joined by "then") run in order,
# and each step gets the result of the step before it.

_THEN = re.compile(r"\s*\n+\s*|\s*[,;.]\s*(?:and\s+)?then\s+|\s+and\s+then\s+", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")


def _split(text: str) -> list[str]:
    parts = [_BULLET.sub("", p).strip().rstrip(".") for p in _THEN.split(text)]
    parts = [p for p in parts if p]
    if not 1 < len(parts) <= settings.get("plan_steps") or any(p.endswith(":") for p in parts):
        return [text]  # one request (or a pasted message body, which must stay whole)
    return parts


async def _wake_plans() -> None:
    if _ends:
        async with _ends:
            _ends.notify_all()


async def _after(t: Task) -> bool:
    """Waits for the step before; False if it didn't finish (stopped, error), so this one is skipped."""
    global _ends
    p = t.after
    _ends = _ends or asyncio.Condition()
    async with _ends:
        await _ends.wait_for(lambda: p.state not in ("working", "queued", "needs_ok"))
    return p.state == "done"


def _with_previous(t: Task, text: str) -> str:
    p = t.after
    return (f"{text}\n\n(This is step {t.step} of {t.of} of a plan. The step before it was "
            f"\u201c{p.text}\u201d and its result was:\n{p.reply[:4000]})")


async def _drive(t: Task, text: str) -> None:
    if not MOCK:
        import desktop_tools as dt
        ctx = {"snap": ""}  # this task's own snapshot ids (and Dia tab)
        dt._ctx.set(ctx)
        _task_ctx[str(t.id)] = ctx
    try:
        first = not t.via
        if first and t.after:
            if not await _after(t):
                t.state, t.reply = "stopped", f"Skipped: step {t.after.step} didn't finish."
                t.emit(type="stopped", text=t.reply)
                return
            t.set("working")
        if first and await (_mock_route if MOCK else _route)(t, text):
            return
        await _acquire(t, _lock_key(t))
        if t.state != "working":
            t.set("working")
        await (_mock_work if MOCK else _work)(t, _with_previous(t, text) if first and t.after else text)
    except asyncio.CancelledError:
        t.state = "stopped"
        t.emit(type="stopped")
        raise
    except Exception as e:
        t.state = "error"
        t.emit(type="error", text=f"Something went wrong: {str(e)[:300]}")
    finally:
        await _release(t)
        t.emit(type="end", state=t.state)
        _save()
        await _wake_plans()
        if t.auto and not MOCK and t.state in ("done", "needs_ok"):
            _notify(t.auto + " is ready", (t.reply or "")[:120])


# ---- saved state: cards, their transcripts (so a waiting "Send it?" can still be answered) and
# the shared history survive a restart. logs/ is git-ignored and stays on this Mac.

def _save() -> None:
    data = {"history": history, "tasks": [dict(t.info(), history=t.history if t.state == "needs_ok" else None)
                                          for t in tasks.values()]}
    with suppress(Exception):
        STATE.parent.mkdir(exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str))
        os.chmod(tmp, 0o600)
        tmp.replace(STATE)


def _load() -> None:
    global _seq
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return
    history[:] = data.get("history", [])
    _trim(history)
    for d in data.get("tasks", [])[-MAX_TASKS:]:
        t = Task(int(d["tid"]), d.get("text", ""))
        t.state, t.kind, t.app, t.via = d.get("state", "done"), d.get("kind", ""), d.get("app", ""), d.get("via", "")
        t.reply, t.auto, t.history = d.get("reply", ""), d.get("auto", ""), d.get("history")
        t.plan, t.step, t.of, t.said = d.get("plan", 0), d.get("step", 0), d.get("of", 0), d.get("said", "")
        t.at, t.checkin, t.acts = d.get("at") or t.at, d.get("checkin", ""), d.get("acts") or []
        if t.state in ("working", "queued") or (t.state == "needs_ok" and t.history is None):
            t.state, t.reply = "stopped", t.reply or "Stopped: Jarvis restarted. Ask again if you still need it."
        tasks[t.id] = t
    _seq = max(tasks, default=0)


def _notify(title: str, text: str) -> None:
    """A silent macOS notification."""
    q = lambda s: '"' + s.replace("\\", "").replace('"', "'") + '"'
    with suppress(Exception):
        subprocess.Popen(["osascript", "-e", f"display notification {q(text)} with title {q(title)}"])


async def _briefing_loop() -> None:
    import briefing
    while True:
        await asyncio.sleep(30)
        with suppress(Exception):
            at = briefing.due()
            if at:
                _start("Brief me", auto=f"Daily briefing ({at})")


checkin_status = {"why": "", "at": 0.0}  # why the last check-in round sent nothing


def _open_checkin(key: str, text: str, title: str) -> Task:
    """A reminder or check-in card: the question, with Keep / Snooze / Drop. Nothing runs."""
    global _seq
    _seq += 1
    t = tasks[_seq] = Task(_seq, text)
    t.state, t.kind, t.auto, t.reply, t.checkin = "checkin", "checkin", title, text, key
    t.emit(type="created", text=text, state="checkin", kind="checkin", auto=title, reply=text)
    _save()
    _notify(f"Jarvis {title.lower()}", text)
    return t


def _apply_checkin(t: Task, answer: str, how: str) -> str:
    verdict = checkins.VERDICT.match(answer)[1].lower()
    msg = checkins.answer(t.checkin, verdict)
    decisions.record("checkin", "", verdict=verdict, question=t.text, key=t.checkin, how=how)
    t.emit(type="followup", text=verdict.capitalize())
    t.state, t.reply = "done", f"{t.text}\n\nYou: {verdict.capitalize()}\n{msg}"
    t.emit(type="reply", text=msg, ask=False)
    _save()
    return msg


def _checkin_answer(answer: str, about: str = "") -> str:
    """answer_checkin tool (voice or typed): find the one open check-in it's about."""
    if not checkins.VERDICT.match(answer or ""):
        return "The answer has to be keep, snooze or drop."
    open_ = [x for x in tasks.values() if x.state == "checkin"]
    if about:
        words = about.lower().split()
        open_ = [x for x in open_ if all(w in x.text.lower() for w in words)]
    if not open_:
        return "No check-in like that is waiting."
    if len(open_) > 1:
        return "Several are open: " + " | ".join(x.text for x in open_) + " Which one?"
    return _apply_checkin(open_[0], answer, "agent")


def _meeting_now() -> str:
    import calendar_tools
    try:
        return calendar_tools.busy_now()
    except Exception:
        return ""


async def _warm_loop() -> None:
    """Keeps any local model in use loaded, so a request after a break isn't slow."""
    while True:
        with suppress(Exception):
            await asyncio.to_thread(settings.keep_warm)
        await asyncio.sleep(240)


async def _checkin_loop() -> None:
    """Reminders fire on time. Check-ins about quiet work go out every 5 min at most, only when allowed."""
    last = 0.0
    while True:
        await asyncio.sleep(20)
        with suppress(Exception):
            for r in checkins.due_reminders():
                _open_checkin(f"rem:{r['id']}", f"Reminder: {r['what']}", "Reminder")
            if time.time() - last < 300:
                continue
            last = time.time()
            open_ = {x.checkin for x in tasks.values() if x.state == "checkin"}
            why = "3 check-ins are already waiting for you" if len(open_) >= 3 else checkins.blocked(
                meeting=await asyncio.to_thread(_meeting_now), idle=await asyncio.to_thread(checkins.idle_seconds))
            if not why:
                try:
                    found = [c for c in await asyncio.to_thread(checkins.slipping) if c["key"] not in open_]
                except Exception:
                    found, why = [], "Mission Control isn't running"
                if found:
                    _open_checkin(found[0]["key"], found[0]["text"], "Check-in")
                    checkins.mark_sent()
                elif not why:
                    why = "nothing has gone quiet"
            checkin_status.update(why=why, at=time.time())


async def _watch_loop() -> None:
    """Page watches: due ones are re-read in the cloud; one whose condition is met becomes a card and a text."""
    import cloud_form
    import messages_tools
    import watch_web
    while True:
        await asyncio.sleep(60)
        with suppress(Exception):
            await cloud_form.close_idle()
        if MOCK:
            continue
        with suppress(Exception):
            for w, detail in await watch_web.check_due():
                _open_checkin(f"watch:{w['id']}", f"Page watch: {detail}\n{w['url']}", "Page watch")
                if w.get("text_me"):
                    await asyncio.to_thread(messages_tools.text_me, f"Jarvis: {detail} {w['url']}")


async def _stop_tasks(tid: int = 0) -> bool:
    victims = [t for t in tasks.values() if t.task and not t.task.done() and (not tid or t.id == tid)]
    for t in victims:
        t.task.cancel()
    for t in victims:
        with suppress(BaseException):
            await t.task
    return bool(victims)


async def _voice_loop() -> None:
    try:
        import copilot_voice as cv
    except Exception as e:
        voice["want"] = False
        publish(type="voice", state="error", text=f"Voice couldn't start: {str(e)[:200]}")
        return
    voice["mod"] = cv
    cv.set_emitter(_voice_event)
    while voice["want"]:
        publish(type="voice", state="connecting")
        try:
            cls = cv.VoiceCopilot
            if settings.get("voice_engine") == "local":
                import local_voice
                cls = local_voice.LocalVoice
            vc = voice["vc"] = cls(prebuffer=voice.pop("prebuffer", False))
            vc.muted = settings.get("voice_muted")
            await vc.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            publish(type="voice", state="warn", text=f"Voice dropped: {str(e)[:200]}")
            with suppress(Exception):  # mics/speakers changed (e.g. an iPhone's mic appeared): re-read the device list
                cv.sd._terminate()
                cv.sd._initialize()
        finally:
            voice["vc"] = None
            cv.ui_talk.clear()
        if voice["want"]:
            await asyncio.sleep(3)


async def _set_voice(on: bool) -> None:
    voice["want"] = on
    if MOCK:
        publish(type="voice", state="ready" if on else "off")
        return
    t = voice["task"]
    if on and not (t and not t.done()):
        voice["task"] = asyncio.create_task(_voice_loop())
    elif not on and t and not t.done():
        t.cancel()
        with suppress(BaseException):
            await t
    if not on:
        publish(type="voice", state="off")


async def _auto_voice_on() -> None:
    """Holding to talk while voice is off turns it on, until it's unused for AUTO_OFF_S."""
    voice.update(auto=True, prebuffer=True, last=time.monotonic())
    if not MOCK:
        try:
            import copilot_voice as cv
        except Exception as e:
            publish(type="voice", state="error", text=f"Voice couldn't start: {str(e)[:200]}")
            return
        voice["mod"] = cv
        publish(type="voice", state="connecting")  # so this same press counts while it connects
    await _set_voice(True)
    if not (voice.get("auto_task") and not voice["auto_task"].done()):
        voice["auto_task"] = asyncio.create_task(_auto_off())


async def _talk_keys_watch() -> None:
    """Holding Option + Command in any app starts voice when it's off (it turns itself off after
    AUTO_OFF_S unused). While voice is on, copilot_voice reads the keys itself."""
    if MOCK or sys.platform != "darwin":
        return
    try:
        import copilot_voice as cv
    except Exception:
        return
    while True:
        await asyncio.sleep(0.05)
        if not voice["want"] and voice["state"] in ("off", "error", "warn") and cv.talk_keys_held():
            print("🎙️  Option + Command held: starting voice")
            await _auto_voice_on()
            await asyncio.sleep(1)


async def _auto_off() -> None:
    while voice["auto"] and voice["want"]:
        await asyncio.sleep(5)
        mod, vc = voice["mod"], voice["vc"]
        job = mod and mod._current_job
        if ((vc and vc.speaking) or (job and not job.done())
                or (mod and (mod.ui_talk.is_set() or mod.talk_keys_held()))):
            voice["last"] = time.monotonic()
        elif voice["auto"] and time.monotonic() - voice["last"] > AUTO_OFF_S:
            await _set_voice(False)


async def _stop_voice() -> bool:
    mod, vc = voice["mod"], voice["vc"]
    stopped = bool(mod) and await mod.stop_job()
    turn = voice.pop("mock_turn", None)
    if turn and not turn.done():
        turn.cancel()
        stopped = True
    if vc:
        await vc.stop_speaking()
    return stopped


@asynccontextmanager
async def lifespan(_app):
    if START_VOICE:
        await _set_voice(True)
    brief = asyncio.create_task(_briefing_loop())
    with suppress(ImportError):  # mock mode runs without the Agents SDK
        import jarvis_tools
        jarvis_tools.HOOKS.update(answer=_checkin_answer,
                                  open=lambda: [x.text for x in tasks.values() if x.state == "checkin"])
    nudges = asyncio.create_task(_checkin_loop())
    watches = asyncio.create_task(_watch_loop())
    keys = asyncio.create_task(_talk_keys_watch())
    warm = asyncio.create_task(_warm_loop())
    yield
    warm.cancel()
    brief.cancel()
    nudges.cancel()
    watches.cancel()
    keys.cancel()
    _save()
    await _set_voice(False)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _guard(request: Request) -> None:
    """Only this page may drive the Mac. Other websites can't send the custom header
    (no CORS is allowed), and the Host/Origin checks block DNS-rebinding tricks."""
    if request.headers.get("x-mac-copilot") != "1":
        raise HTTPException(403)
    if MOCK:
        return
    origin = request.headers.get("origin", "")
    if not _our_host(request.headers.get("host", "")) or (origin and not _our_host(origin.split("://", 1)[-1])):
        raise HTTPException(403)


def _our_host(host: str) -> bool:
    return host in LOCAL_HOSTS or bool(TAILNET) and host.split(":", 1)[0] == TAILNET


def _tailnet_name() -> str:
    """This Mac's name on the user’s tailnet (e.g. example.tailnet.ts.net), or "" without Tailscale."""
    for cli in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "tailscale"):
        with suppress(Exception):
            out = subprocess.run([cli, "status", "--self", "--json"], capture_output=True, text=True, timeout=5).stdout
            return (json.loads(out).get("Self") or {}).get("DNSName", "").rstrip(".")
    return ""


_MOCK_APPS = {"slack": "Slack", "notion": "Notion", "music": "Music", "text": "Messages",
              "mail": "Mail", "calendar": "Calendar"}


async def _mock_helper(job, i: int, app: str) -> None:
    hid = f"h{i + 1}"
    job.emit(type="helper", id=hid, app=app, state="start", text=f"See what needs attention in {app}")
    steps = [("open_app", {"app_name": app}), ("look_at_app", {"app_name": app, "filter": ""}),
             ("read_app_text", {"app_name": app, "filter": ""})]
    for name, args in steps[:random.randint(2, 3)]:
        await asyncio.sleep(random.uniform(0.5, 1.4))
        job.emit(type="helper", id=hid, app=app, state="tool", name=name, args=json.dumps(args))
    await asyncio.sleep(random.uniform(0.3, 0.9))
    job.emit(type="helper", id=hid, app=app, state="done", text=f"Mock findings from {app}.")


_SIMPLE = re.compile(r"\b(volume|dark mode|light mode|mute|quit|open)\b", re.I)


async def _mock_run(job, text: str) -> None:
    await asyncio.sleep(0.12)
    if _SIMPLE.search(text) and len(text.split()) <= 6:
        job.emit(type="route", via="jev", ms=random.randint(95, 140), route="direct action")
        job.emit(type="reply", text="Done. (Mock mode: nothing changed on a Mac.)")
        return
    job.emit(type="route", via="gpt-5.5", ms=random.randint(100, 130))
    apps = [a for k, a in _MOCK_APPS.items() if k in text.lower()]
    if len(apps) >= 2:
        await asyncio.sleep(0.7)
        job.emit(type="tool", name="run_helpers",
                 args=json.dumps({"jobs": [{"app": a, "task": "what needs attention"} for a in apps]}))
        await asyncio.gather(*(_mock_helper(job, i, a) for i, a in enumerate(apps)))
        await asyncio.sleep(0.5)
        job.emit(type="reply", text=f"Mock mode: {len(apps)} helpers checked {', '.join(apps)} at "
                                    "the same time. Nothing was done on a Mac.")
        return
    steps = [("open_app", {"app_name": "Calculator"}),
             ("look_at_app", {"app_name": "Calculator", "filter": ""}),
             ("click", {"ref": "e15"}), ("click", {"ref": "e9"}), ("click", {"ref": "e21"}),
             ("read_app_text", {"app_name": "Calculator", "filter": ""})]
    for name, args in steps:
        await asyncio.sleep(0.8)
        job.emit(type="tool", name=name, args=json.dumps(args))
    await asyncio.sleep(0.6)
    job.emit(type="reply", text="Mock mode: this is what a gpt-5.5 task looks like. "
                                "Nothing was done on a Mac.")


_SEND = re.compile(r"\b(reply|send|post|tell|email|text)\b", re.I)
_READ = re.compile(r"^\s*(what|what's|how|any|who|check|brief|read|summari[sz]e|is|are|did|show)\b", re.I)


async def _mock_route(t: Task, text: str) -> bool:
    await asyncio.sleep(0.12)
    ms = random.randint(95, 140)
    if _SIMPLE.search(text) and len(text.split()) <= 6:
        t.via, t.kind, t.app = "jev", "work", ""
        t.emit(type="route", via="jev", ms=ms, route="direct action", kind="work", app="")
        _finish(t, text, "Done. (Mock mode: nothing changed on a Mac.)", False)
        return True
    t.via = settings.label("work")
    if _SEND.search(text):
        t.kind, t.app = "send", ""
    elif _READ.search(text):
        t.kind, t.app = "read", ""
    else:
        t.kind = "work"
        t.app = next((a for k, a in _MOCK_APPS.items() if k in text.lower()), "Calculator")
    t.emit(type="route", via=t.via, ms=ms, route="llm", kind=t.kind, app=t.app)
    return False


async def _mock_steps(t: Task, steps: list) -> None:
    for name, args in steps:
        await asyncio.sleep(random.uniform(0.5, 1.2))
        t.emit(type="tool", name=name, args=json.dumps(args))


async def _mock_work(t: Task, text: str) -> None:
    if t.history is not None:
        if YES.search(text):
            msg = text.split("\n\n", 1)[1] if "instead of your draft" in text else "Yes, the venue is confirmed for Thursday at 6."
            await _mock_steps(t, [("slack_send", {"to": "Sam Example", "message": msg})])
            _finish(t, text, "Sent to Sam. (Mock mode: nothing was sent.)", False)
        else:
            _finish(t, text, "OK, I won't send it.", False)
        return
    if t.kind == "send":
        await _mock_steps(t, [("slack_inbox", {"days": 2}), ("slack_read", {"conversation": "Sam Example", "limit": 20})])
        t.history = []
        t.draft = {"channel": "slack", "to": "Sam Example", "subject": "", "message": "Yes, the venue is confirmed for Thursday at 6."}
        t.emit(type="draft", **t.draft)
        _finish(t, text, "Sam asked whether the venue is confirmed for Thursday. I'll send Sam this Slack DM:"
                         "\n\n\u201cYes, the venue is confirmed for Thursday at 6.\u201d\n\nSend it?", True)
        return
    if t.kind == "read":
        await _mock_steps(t, random.sample([("slack_inbox", {"days": 2}), ("email_inbox", {"days": 2}),
                                            ("agent_status", {"provider": "all"}), ("recent_texts", {"limit": 15})], 2))
        _finish(t, text, "Mock mode: here's what I found. Sam is waiting on a Slack reply and one Devin session needs you.", False)
        return
    await _mock_steps(t, [("open_app", {"app_name": t.app}), ("look_at_app", {"app_name": t.app, "filter": ""}),
                          ("click", {"ref": "e15"}), ("read_app_text", {"app_name": t.app, "filter": ""})])
    _finish(t, text, f"Mock mode: finished in {t.app}. Nothing was done on a Mac.", False)


MOCK_SPOKEN = "Open Calculator and work out 12 times 12"


class _VoiceJob:
    def emit(self, **event) -> None:
        publish(src="voice", **event)


async def _mock_levels() -> None:
    while True:
        publish(src="voice", type="level", v=round(random.uniform(0.15, 0.9), 3))
        await asyncio.sleep(0.12)


async def _mock_voice_turn() -> None:
    await asyncio.sleep(0.6)
    publish(src="voice", type="heard", text=MOCK_SPOKEN)
    publish(src="voice", type="speaking", on=True)
    await asyncio.sleep(0.7)
    publish(src="voice", type="said", text="On it!")
    publish(src="voice", type="speaking", on=False)
    job = _VoiceJob()
    job.emit(type="task", text=MOCK_SPOKEN)
    try:
        await _mock_run(job, MOCK_SPOKEN)
    except asyncio.CancelledError:
        job.emit(type="stopped")
        job.emit(type="end")
        raise
    job.emit(type="end")
    publish(src="voice", type="speaking", on=True)
    await asyncio.sleep(1.8)
    publish(src="voice", type="said", text="12 times 12 is 144. (Mock mode: nothing was done.)")
    publish(src="voice", type="speaking", on=False)


async def _talk(on: bool) -> bool:
    """The page's mic button. Released automatically after MAX_TALK_S in case the page vanishes."""
    if voice["release"]:
        voice["release"].cancel()
        voice["release"] = None
    if on and voice["state"] in ("off", "error"):
        await _auto_voice_on()
    if MOCK:
        if voice["state"] != "ready":
            return False
        if on and not voice.get("levels"):
            publish(src="voice", type="listening", on=True)
            voice["levels"] = asyncio.create_task(_mock_levels())
        elif not on and voice.get("levels"):
            voice.pop("levels").cancel()
            publish(src="voice", type="listening", on=False, sent=True)
            voice["mock_turn"] = asyncio.create_task(_mock_voice_turn())
        return True
    mod = voice["mod"]
    if not mod or voice["state"] not in ("ready", "connecting"):
        return False
    if on:
        mod.ui_talk.set()
        voice["release"] = asyncio.get_running_loop().call_later(MAX_TALK_S, mod.ui_talk.clear)
    else:
        mod.ui_talk.clear()
    return True


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/{name}", include_in_schema=False)
def icon(name: str) -> FileResponse:
    """Jarvis's logo: favicon, the phone home-screen icon and the full-size mark."""
    if name not in ("favicon.svg", "logo.svg"):
        raise HTTPException(404)
    return FileResponse(WEB / name)


_perms: dict = {"at": 0.0, "value": {}}


def _permissions() -> dict:
    """What macOS lets Mac Copilot do: accessibility (see and click apps) and screen recording."""
    if time.time() - _perms["at"] < 15:
        return _perms["value"]
    value = {}
    with suppress(OSError, ValueError, subprocess.TimeoutExpired):
        out = subprocess.run(["agent-desktop", "permissions"], capture_output=True, text=True, timeout=10).stdout
        data = json.loads(out).get("data") or {}
        value = {k: (data.get(k) or {}).get("state", "unknown") for k in ("accessibility", "screen_recording")}
    _perms.update(at=time.time(), value=value)
    return value


def _mission_control() -> bool:
    import urllib.request
    with suppress(OSError):
        urllib.request.urlopen("http://localhost:4777", timeout=1).close()
        return True
    return False


def _connections() -> dict:
    """What's connected, as on/off only. Never includes keys or tokens."""
    if MOCK:
        return {"openai": True, "jev": True, "slack": True, "gmail": True, "dia": True,
                "mission_control": True, "accessibility": True, "screen_recording": True, "gcal_add": True}
    from gmail_tools import TOKEN_FILE
    from calendar_tools import google_can_add
    perms = _permissions()
    return {"openai": bool(os.environ.get("OPENAI_API_KEY")), "jev": bool(os.environ.get("TYPESAFE_API_KEY")),
            "slack": bool(os.environ.get("SLACK_USER_TOKEN")), "tinyfish": bool(os.environ.get("TINYFISH_API_KEY")), "gmail": TOKEN_FILE.exists(),
            "dia": _browser()["connected"], "mission_control": _mission_control(),
            "accessibility": perms.get("accessibility") == "granted",
            "screen_recording": perms.get("screen_recording") == "granted", "gcal_add": google_can_add()}


_TEXT_TOOLS = ("send_text", "read_texts", "recent_texts", "search_texts")


def _channel(t: Task) -> str:
    """What a waiting task is about to do, from the tools it used. "text" keeps it off Claude Mem."""
    names = [str(i.get("name", "")) for i in (t.history or []) if isinstance(i, dict) and i.get("type") == "function_call"]
    if any(n in _TEXT_TOOLS for n in names):
        return "text"
    for n, ch in (("slack", "slack"), ("email", "email"), ("agent", "agent"), ("form_", "form"), ("web_", "web")):
        if any(n in x for x in names):
            return ch
    return t.app.lower() if t.app else ""


@app.post("/api/checkin")
async def checkin_answer(request: Request) -> dict:
    """Keep / Snooze / Drop on a reminder or check-in card."""
    _guard(request)
    body = await request.json()
    t = tasks.get(int(body.get("task") or 0))
    answer = str(body.get("answer", ""))
    if not t or t.state != "checkin":
        return {"error": "That check-in was already answered."}
    if not checkins.VERDICT.match(answer):
        raise HTTPException(400, "keep, snooze or drop")
    return {"tid": t.id, "checkin": _apply_checkin(t, answer, "card")}


@app.get("/api/decisions/export")
def decisions_export(request: Request, texts: int = 0) -> list:
    """The decision log as training rows. Texts are left out unless texts=1."""
    _guard(request)
    return decisions.export(bool(texts))


def _settings() -> dict:
    import briefing
    b = briefing.load()
    return {"limits": settings.load(), "ranges": settings.LIMITS, "briefing": {"on": b["on"], "time": b["time"]},
            "memory": {"turns": len(history) // 2}, "decisions": decisions.count(), "rules": rules.load(), "people": people.load(),
            "models": {"choices": settings.MODELS, "local": settings.local_models()},
            "checkins": {**checkins.load()["settings"], "today": checkins.sent_today(), "why": checkin_status["why"],
                         "reminders": len(checkins.upcoming())}}


@app.get("/api/settings")
async def get_settings(request: Request) -> dict:
    _guard(request)
    return {**_settings(), "connections": await asyncio.to_thread(_connections)}


@app.post("/api/settings")
async def set_settings(request: Request) -> dict:
    _guard(request)
    import briefing
    body = await request.json()
    try:
        settings.update({k: body[k] for k in settings.LIMITS if k in body})
        settings.update({k: body[k] for k in ("model_quick", "model_work", "model_voice", "model_brain", "voice_engine", "voice_speed")
                         if k in body})
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e) if str(e).startswith("Pick") else "Numbers only.")
    if "people" in body:
        if not isinstance(body["people"], list):
            raise HTTPException(400, "people must be a list")
        people.save(body["people"][:1000])
    if "rules" in body:
        if not isinstance(body["rules"], list):
            raise HTTPException(400, "rules must be a list")
        rules.save([str(r) for r in body["rules"]][:200])
    ci = {new: body[old] for old, new in (("checkin_on", "on"), ("checkin_cap", "cap"), ("quiet_from", "quiet_from"),
                                          ("quiet_to", "quiet_to"), ("stale_hours", "stale_hours")) if old in body}
    if ci:
        try:
            checkins.change(**ci)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e) if str(e).startswith("Use") else "Numbers only.")
    if "briefing_on" in body or "briefing_time" in body:
        msg = briefing.change(str(body.get("briefing_time", "")), "" if "briefing_on" not in body
                              else ("on" if body["briefing_on"] else "off"))
        if msg.startswith("Use "):
            raise HTTPException(400, msg)
    _trim(history)
    _save()
    return _settings()


@app.post("/api/memory/clear")
async def clear_memory(request: Request) -> dict:
    """Forgets earlier conversation; the cards on the page stay."""
    _guard(request)
    history.clear()
    _save()
    if voice["mod"]:
        voice["mod"].reset_history()
    return _settings()


@app.get("/api/status")
def status() -> dict:
    return {"mock": MOCK, "jev": MOCK or bool(os.environ.get("TYPESAFE_API_KEY")),
            "model": settings.label("work"), "voice": voice["state"], "demo": os.environ.get("MC_DEMO") == "1",
            "permissions": _permissions(), "browser": _browser()}


def _browser() -> dict:
    from browser_bridge import BRIDGE
    return {"connected": BRIDGE.connected, "version": BRIDGE.version}


@app.post("/api/permissions/guide")
def permissions_guide(request: Request) -> dict:
    """Opens System Settings with a panel showing which app to allow (asked for from Setup)."""
    _guard(request)
    _perms["at"] = 0.0
    r = subprocess.run(["agent-desktop", "permissions", "--guide"], capture_output=True, text=True, timeout=20)
    return {"ok": r.returncode == 0, "error": None if r.returncode == 0 else (r.stdout or r.stderr).strip()[:200]}


@app.post("/api/people/draft")
async def people_draft(request: Request) -> dict:
    """Suggested people: who the user texts, emails and Slacks most (counts only). Nothing is saved."""
    _guard(request)
    if MOCK:
        return {"people": [{"name": "Riley Example", "phone": "+12025550101", "texts": 212, "emails": 0, "slack_msgs": 0},
                           {"name": "Morgan Example", "email": "morgan@example.invalid", "slack": "U0MOCK1", "texts": 0,
                            "emails": 14, "slack_msgs": 41}], "errors": ["Slack: mock"]}
    return await asyncio.to_thread(people.draft)


@app.post("/api/people/fill")
async def people_fill(request: Request) -> dict:
    """Contact details saved people are missing, found in texts, email and Slack. Nothing is saved."""
    _guard(request)
    if MOCK:
        return {"fills": [{"name": p["name"], "email": p["name"].split()[0].lower() + "@example.com"}
                          for p in people.load() if not p.get("email")][:3], "errors": []}
    return await asyncio.to_thread(people.fill)


_google_signin: dict = {"proc": None}


@app.post("/api/connect/google")
def connect_google(request: Request) -> dict:
    """Opens Google's sign-in page in the browser (Gmail + Calendar); the token is saved when the user clicks Allow."""
    _guard(request)
    p = _google_signin["proc"]
    if p and p.poll() is None:
        return {"ok": True}
    if MOCK:
        return {"ok": True}
    log = open(HERE / "logs" / "google_signin.log", "w")
    _google_signin["proc"] = subprocess.Popen([sys.executable, str(HERE / "gmail_auth.py")], cwd=HERE,
                                              stdout=log, stderr=subprocess.STDOUT)
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    _guard(request)
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)
    q.put_nowait({"type": "voice", "state": voice["state"]})

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    event = {"type": "ping"}
                yield json.dumps(event) + "\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/preview")
def preview(request: Request) -> dict:
    _guard(request)
    cut = time.time() - 180
    for k in [k for k, v in _surfaces.items() if v["at"] < cut]:
        _surfaces.pop(k)
    return {"surfaces": sorted(_surfaces.values(), key=lambda v: -v["at"])}


_MOCK_SHOT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 800"><rect width="1280" height="800" rx="18" fill="#1d1f24"/>
<rect width="1280" height="52" rx="18" fill="#2a2c32"/><circle cx="34" cy="26" r="9" fill="#ff5f57"/><circle cx="62" cy="26" r="9" fill="#febc2e"/>
<circle cx="90" cy="26" r="9" fill="#28c840"/><text x="640" y="34" fill="#ddd" font-family="sans-serif" font-size="22" text-anchor="middle">{app}</text>
<rect x="0" y="52" width="360" height="748" fill="#24262b"/><rect x="24" y="{y}" width="312" height="60" rx="10" fill="#2f6bf1"/>
<text x="400" y="120" fill="#888" font-family="sans-serif" font-size="22">Test window (fake) · {t}</text></svg>"""


async def _dia_shot(tid: str) -> Response:
    """A picture of the background Dia tab this task works in, from the Jarvis for Dia extension."""
    from browser_bridge import BRIDGE
    tab = (_task_ctx.get(str(tid)) or {}).get("tab")
    if not tab:
        raise HTTPException(404, "no Dia tab yet")
    try:
        data = await asyncio.wait_for(BRIDGE.call("shot", tab=tab), 6)
    except Exception as e:  # noqa: BLE001
        if "Unknown command" in str(e):
            raise HTTPException(426, "reload the Jarvis for Dia extension")
        raise HTTPException(409, "couldn't picture the tab")
    if not isinstance(data, str) or not data:
        raise HTTPException(409, "couldn't picture the tab")
    return Response(base64.b64decode(data), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/preview/shot")
async def preview_shot(request: Request, app_name: str = "", web: int = 0, tid: str = "") -> Response:
    _guard(request)
    if web:
        if MOCK:
            app_name = "Dia"
        else:
            return await _dia_shot(tid)
    if MOCK:
        svg = _MOCK_SHOT.format(app=app_name.replace("<", ""), y=80 + int(time.time()) % 5 * 70, t=time.strftime("%H:%M:%S"))
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    from screen_tools import _pick
    w = await asyncio.to_thread(_pick, app_name)
    if not w:
        raise HTTPException(404, f"{app_name} has no window on screen")
    path = Path(tempfile.gettempdir()) / f"jarvis_preview_{w[0]}.jpg"
    try:
        proc = await asyncio.create_subprocess_exec("screencapture", "-x", "-o", "-t", "jpg", "-l", str(w[0]), str(path),
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode or not path.exists():
            raise HTTPException(500, "couldn't capture the window")
        await (await asyncio.create_subprocess_exec("sips", "-Z", "1280", "-s", "formatOptions", "60", str(path),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/talk")
async def talk(request: Request) -> dict:
    _guard(request)
    body = await request.json()
    return {"ok": await _talk(bool(body.get("on"))), "voice": voice["state"]}


@app.post("/api/voice/hush")
async def hush(request: Request) -> dict:
    """Stops what Jarvis is saying out loud (tasks carry on). {"mute": true/false} keeps it quiet until unmuted."""
    _guard(request)
    body = await _body(request)
    if "mute" in body:
        settings.update({"voice_muted": bool(body["mute"])})
    muted, vc = settings.get("voice_muted"), voice["vc"]
    if vc:
        vc.muted = muted
    if "mute" not in body or muted:
        if vc:
            vc.hush()
        if MOCK:
            publish(src="voice", type="speaking", on=False)
    return {"muted": muted}


@app.post("/api/voice")
async def set_voice(request: Request) -> dict:
    _guard(request)
    body = await request.json()
    voice["auto"] = False
    await _set_voice(bool(body.get("on")))
    return {"voice": voice["state"]}


@app.post("/api/voice/clip")
async def voice_clip(request: Request) -> dict:
    """Writes down a recording from the phone's microphone; the page then sends the words like a typed
    request. The audio is only passed to OpenAI to transcribe, never saved."""
    _guard(request)
    audio = await request.body()
    if not audio or len(audio) > 10_000_000:
        raise HTTPException(400, "no audio")
    if MOCK:
        return {"text": "What's on my calendar today?"}
    ctype = request.headers.get("content-type", "audio/webm").split(";")[0].strip()
    ext = {"audio/mp4": "m4a", "audio/aac": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg"}.get(ctype, "webm")
    if settings.get("voice_engine") == "local":
        try:
            import local_voice
            if await local_voice.up(30):
                return {"text": await local_voice.hear(audio, ctype)}
        except Exception as e:  # noqa: BLE001
            print(f"phone clip (this Mac): {str(e)[:200]}, using OpenAI")
    from openai import AsyncOpenAI
    try:
        r = await AsyncOpenAI().audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=(f"clip.{ext}", audio, ctype))
    except Exception as e:  # noqa: BLE001
        print(f"phone clip: {str(e)[:200]}")
        raise HTTPException(502, "couldn't write that down")
    return {"text": (r.text or "").strip()}


@app.post("/api/voice/say")
async def voice_say(request: Request) -> Response:
    """Jarvis's reply as speech for the phone to play (the Mac's own voice only plays on the Mac)."""
    _guard(request)
    text = str((await _body(request)).get("text") or "").strip()[:1500]
    if not text:
        raise HTTPException(400, "nothing to say")
    if MOCK:
        n = 48000  # three seconds of silence at 16 kHz
        wav = b"RIFF" + (36 + 2 * n).to_bytes(4, "little") + b"WAVEfmt " + bytes.fromhex("1000000001000100803e0000007d000002001000") \
            + b"data" + (2 * n).to_bytes(4, "little") + bytes(2 * n)
        return Response(wav, media_type="audio/wav")
    if settings.get("voice_engine") == "local":
        try:
            import local_voice
            if await local_voice.up(30):
                return Response(await local_voice.say_wav(text), media_type="audio/wav")
        except Exception as e:  # noqa: BLE001
            print(f"phone say (this Mac): {str(e)[:200]}, using OpenAI")
    from openai import AsyncOpenAI
    try:
        r = await AsyncOpenAI().audio.speech.create(model="gpt-4o-mini-tts", voice="ash", input=text, response_format="mp3",
                                                    instructions="You are JARVIS, a calm, quick, friendly assistant. Speak naturally and briskly.")
    except Exception as e:  # noqa: BLE001
        print(f"phone say: {str(e)[:200]}")
        raise HTTPException(502, "couldn't speak that")
    return Response(r.content, media_type="audio/mpeg")


@app.post("/api/demo")
async def set_demo(request: Request) -> dict:
    """Demo mode: helper cursors on screen, private details blurred on the page and kept short."""
    _guard(request)
    on = bool((await request.json()).get("on"))
    os.environ["MC_DEMO"] = os.environ["MC_HELPER_CURSORS"] = "1" if on else "0"
    return {"demo": on}


@app.post("/api/command")
async def command(request: Request) -> dict:
    """Starts a task (or answers one that's waiting for an OK) and returns at once; its events
    stream over /api/events."""
    _guard(request)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        raise HTTPException(400, "empty command")
    tid = int(body.get("task") or 0)
    t = tasks.get(tid)
    if checkins.VERDICT.match(text) and len(text.split()) <= 4 and (not t or t.state == "checkin"):
        if not t:
            open_ = [x for x in tasks.values() if x.state == "checkin"]
            if len(open_) > 1:
                return {"error": f"{len(open_)} check-ins are open. Answer on the card."}
            t = open_[0] if open_ else None
        if t:
            return {"tid": t.id, "checkin": _apply_checkin(t, text, "typed")}
    if t and t.state == "checkin":
        return {"tid": tid, "error": "Answer keep, snooze or drop."}
    if tid and (not t or t.state != "needs_ok"):
        return {"tid": tid, "error": "That task isn't waiting for an answer any more."}
    if not t and len(text.split()) <= 6 and (YES.search(text) or NO.search(text)):
        waiting = [x for x in tasks.values() if x.state == "needs_ok"]
        if len(waiting) == 1:
            t = waiting[0]
        elif waiting:
            return {"error": f"{len(waiting)} tasks are waiting for your OK. Answer on the card, or use Approve all."}
        else:
            return {"error": "Nothing is waiting for your OK."}
    if t:
        edit = str(body.get("edit") or "").strip()[:4000]
        base = {"request": t.text, "draft": t.reply, "msg": t.draft, "run": t.run}
        if edit and t.draft and edit != str(t.draft.get("message", "")).strip():
            t.decision = {**base, "edited": edit, "answer": "yes"}
            decisions.record("answer", _channel(t), verdict="edited", app=t.app, answer="Yes, with my edits",
                             **{k: v for k, v in t.decision.items() if k != "answer"})
            t.emit(type="followup", text="Yes, send my edited version.")
            text = f"Yes, send it, but use exactly this text instead of your draft:\n\n{edit}"
        else:
            t.decision = {**base, "answer": text}
            decisions.record("answer", _channel(t), verdict=decisions.verdict(text), app=t.app, **t.decision)
            t.emit(type="followup", text=text)
        t.set("working")
        t.task = asyncio.create_task(_drive(t, text))
        return {"tid": t.id, "followup": True}
    front = ""
    if body.get("front") and not MOCK:
        from screen_tools import front_note
        front = await asyncio.to_thread(front_note)
    steps = _split(text)
    if len(steps) == 1:
        t = _start(text, brief=bool(body.get("phone")), front=front)
        return {"tid": t.id}
    prev = None
    for i, step in enumerate(steps, 1):
        prev = _start(step, after=prev, step=i, of=len(steps), said=text if i == 1 else "", front=front if i == 1 else "")
    return {"tid": prev.plan, "plan": prev.plan, "steps": len(steps)}


def _start(text: str, auto: str = "", after: Task | None = None, step: int = 0, of: int = 0,
           said: str = "", brief: bool = False, front: str = "") -> Task:
    global _seq
    _seq += 1
    t = tasks[_seq] = Task(_seq, text)
    t.brief, t.front = brief, front
    t.auto, t.after, t.step, t.of, t.said = auto, after, step, of, said
    t.plan = after.plan if after else (t.id if of else 0)
    for old in [x for x in tasks.values() if x.state not in ("working", "queued", "needs_ok", "checkin")][:-MAX_TASKS]:
        tasks.pop(old.id, None)
    if after:
        t.state, t.waiting = "queued", f"step {step - 1}"
    t.emit(type="created", text=text, state=t.state, waiting=t.waiting, auto=auto,
           plan=t.plan, step=step, of=of, said=said)
    t.task = asyncio.create_task(_drive(t, text))
    _save()
    return t


@app.websocket("/api/browser")
async def browser_ws(ws: WebSocket) -> None:
    """The Jarvis for Dia extension dials in here. Only that extension's ID is accepted."""
    from browser_bridge import BRIDGE, EXTENSION_ID
    if ws.headers.get("origin") != f"chrome-extension://{EXTENSION_ID}" or ws.headers.get("host") not in LOCAL_HOSTS:
        await ws.close(code=1008)
        return
    await ws.accept()
    await BRIDGE.serve(ws)


@app.get("/api/tasks")
def list_tasks(request: Request) -> list:
    _guard(request)
    return [t.info() for t in tasks.values()]


async def _body(request: Request) -> dict:
    with suppress(ValueError):
        if await request.body():
            return await request.json()
    return {}


@app.post("/api/stop")
async def stop(request: Request) -> dict:
    """Stops one task ({"task": id}), or everything: every task, the queue and the voice."""
    _guard(request)
    tid = int((await _body(request)).get("task") or 0)
    if tid:
        return {"stopped": await _stop_tasks(tid)}
    typed, spoken = await asyncio.gather(_stop_tasks(), _stop_voice())
    return {"stopped": typed or spoken}


def _archive() -> None:
    """Saves the current conversation to History (only if it has any requests)."""
    done = [dict(t.info(), state="stopped" if t.state in ("working", "queued", "needs_ok") else t.state)
            for t in tasks.values()]
    if not done:
        return
    first = next((d for d in done if not d["auto"]), done[0])
    rec = {"id": f"{int(done[0]['at'] * 1000)}-{done[0]['tid']}", "title": (first["said"] or first["text"]).split("\n")[0][:80],
           "started": done[0]["at"], "ended": done[-1]["at"], "count": len(done), "tasks": done, "history": history}
    CONVERSATIONS.mkdir(parents=True, exist_ok=True)
    f = CONVERSATIONS / (rec["id"] + ".json")
    f.write_text(json.dumps(rec, default=str))
    os.chmod(f, 0o600)


def _conv_file(cid: str) -> Path:
    if not re.fullmatch(r"\d+-\d+", cid):
        raise HTTPException(404)
    f = CONVERSATIONS / (cid + ".json")
    if not f.exists():
        raise HTTPException(404)
    return f


@app.get("/api/conversations")
def conversations(request: Request, q: str = "") -> list:
    """Past conversations, newest first. q searches what you asked and what Jarvis answered."""
    _guard(request)
    out, q = [], q.strip().lower()
    for f in sorted(CONVERSATIONS.glob("*.json"), reverse=True) if CONVERSATIONS.exists() else []:
        with suppress(OSError, ValueError):
            rec = json.loads(f.read_text())
            if q and not any(q in (str(t.get("said") or "") + " " + t.get("text", "") + " " + str(t.get("reply") or "")).lower()
                             for t in rec["tasks"]):
                continue
            out.append({k: rec[k] for k in ("id", "title", "started", "ended", "count")})
    return sorted(out, key=lambda r: r["ended"], reverse=True)[:300]


@app.get("/api/conversations/{cid}")
def conversation(cid: str, request: Request) -> dict:
    _guard(request)
    rec = json.loads(_conv_file(cid).read_text())
    return {k: rec[k] for k in ("id", "title", "started", "ended", "count", "tasks")}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, request: Request) -> dict:
    """Removes a past chat from History. It's kept in logs/conversations/deleted, in case it's wanted back."""
    _guard(request)
    f = _conv_file(cid)
    (CONVERSATIONS / "deleted").mkdir(exist_ok=True)
    f.replace(CONVERSATIONS / "deleted" / f.name)
    return {"ok": True}


@app.post("/api/conversations/{cid}/continue")
async def continue_conversation(cid: str, request: Request) -> dict:
    """Saves the current conversation to History and brings this one back, memory included."""
    global _seq
    _guard(request)
    f = _conv_file(cid)
    rec = json.loads(f.read_text())
    await asyncio.gather(_stop_tasks(), _stop_voice())
    _archive()
    tasks.clear()
    for d in rec["tasks"]:
        t = Task(int(d["tid"]), d.get("text", ""))
        for k in ("state", "kind", "app", "via", "reply", "auto", "plan", "step", "of", "said", "at"):
            if d.get(k) is not None:
                setattr(t, k, d[k])
        tasks[t.id] = t
    _seq = max(_seq, max(tasks, default=0))
    history[:] = rec.get("history", [])
    _trim(history)
    f.unlink()
    _save()
    if voice["mod"]:
        voice["mod"].reset_history()
    return {"ok": True}


@app.post("/api/reset")
async def reset(request: Request) -> dict:
    """New conversation: the current one is saved to History first."""
    _guard(request)
    await asyncio.gather(_stop_tasks(), _stop_voice())
    _archive()
    tasks.clear()
    history.clear()
    _save()
    if voice["mod"]:
        voice["mod"].reset_history()
    return {"ok": True}


def main() -> None:
    global MOCK, AGENT, READ_AGENT, START_VOICE, TAILNET
    p = argparse.ArgumentParser(description="Mac Copilot web page")
    p.add_argument("--mock", action="store_true", help="fake responses; never touches the Mac")
    p.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    p.add_argument("--voice", action="store_true",
                   help="start with the microphone on (otherwise turn it on from the page)")
    a = p.parse_args()
    MOCK = a.mock
    START_VOICE = a.voice
    TAILNET = "" if MOCK else _tailnet_name()
    if MOCK:
        global STATE
        STATE = STATE.with_name("state-mock.json")
    _load()
    if not MOCK:
        from desktop_tools import load_env
        from helpers import MAIN_INSTRUCTIONS, MAIN_TOOLS, READ_INSTRUCTIONS, READ_TOOLS_LIST
        load_env()
        from agents import Agent
        AGENT = Agent(name="Mac Copilot", instructions=rules.instructions(MAIN_INSTRUCTIONS), tools=MAIN_TOOLS, model="gpt-5.5")
        READ_AGENT = Agent(name="Mac Copilot (read only)", instructions=rules.instructions(READ_INSTRUCTIONS),
                           tools=READ_TOOLS_LIST, model="gpt-5.5")
    url = f"http://localhost:{PORT}"
    print(f"Mac Copilot web page: {url}{'  (mock mode)' if MOCK else ''}\n(Ctrl+C here to quit)")
    if not a.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning",
                timeout_graceful_shutdown=2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

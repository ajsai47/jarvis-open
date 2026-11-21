"""Parallel helpers: one gpt-5.5 worker per app, all running at the same time.

Helpers work in the background, each with its own snapshot state. They may make changes that
are easy to undo (drafts, forms, notes, reminders, events) but never send, post, submit or delete;
the main agent does that itself after the user says yes.
On-screen helper cursors (an agent-desktop multi-agent session drawn over the screen) are off
by default so they don't distract; MC_HELPER_CURSORS=1 in .env turns them on.
"""

import asyncio
import contextvars
import datetime
import os
import re
from typing import Callable

from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ToolCallItem
from pydantic import BaseModel

import browser_tools as bt
import cloud_form as cf
import cloud_web as cw
import watch_web as ww
import jarvis_tools as jt
import phone_tools as pt
import desktop_tools as dt
import rules
import settings

EMIT: contextvars.ContextVar[Callable[..., None]] = contextvars.ContextVar(
    "helper_emit", default=lambda **event: None)
COLORS = ["#4299FF", "#FF6FB5", "#43E8A0", "#FFB547", "#B38CFF", "#5FE3FF"]
HELPER_TIMEOUT_S = 150
READ_TOOLS = {"list_open_apps", "open_app", "look_at_app", "read_app_text", "click", "scroll",
              "recent_texts", "read_texts", "search_texts", "list_reminders", "find_files", "list_events",
              "slack_inbox", "slack_read", "slack_search", "email_inbox", "search_email", "read_email",
              "agent_status", "agent_thread"}
READ_TOOLS |= bt.WEB_READ | cw.CLOUD_READ | ww.WATCH_READ
# Easy to undo. press_key stays out: Return sends in most chat apps.
UNDOABLE_TOOLS = READ_TOOLS | bt.WEB_UNDOABLE | {"type_text", "add_reminder", "add_event", "open_website", "open_file"}

HELPER_RULES = """
You are one of several helpers working at the same time, each in a different app. Your app is
{app}. Never touch any other app (texts: use recent_texts / read_texts / search_texts; Slack: slack_inbox / slack_read /
slack_search; email: email_inbox / search_email / read_email; agents: agent_status / agent_thread).
{can}
Finish with a short, factual summary of what you found or did, with names and counts.
"""
CAN_READ = """You only read and look around: open the app, look, click to navigate (a channel, thread, tab or
page), scroll and read. Never type, send, post, reply, delete, buy or change settings, even if
the task asks; say what would need doing instead."""
CAN_UNDOABLE = """You may read, navigate and make changes that are easy to undo: type a draft, fill in a form,
write or edit a note, add a reminder or calendar event. Check each change afterwards. Never
send, post, reply, submit, delete, buy or change settings, and never click a Send, Post, Submit
or Delete button, even if the task asks: leave it ready and say exactly what's ready and the one
step left, so the user can approve it."""

PARALLEL_RULE = """
8. When a request needs reading or checking two or more different apps (e.g. "check Slack and
   Notion", "what needs a reply in Slack and my texts?"), use run_helpers with one job per app
   so they run at the same time, then combine their findings into one short answer. The same
   goes for easy-to-undo work in two or more apps (drafts, notes, forms, reminders). Helpers never
   send, post, submit or delete: do that yourself afterwards, with the usual Safety check.
9. "Brief me" / "what's my day?": in ONE turn call list_events(today), list_reminders(today),
   recent_texts, slack_inbox, email_inbox and agent_status.
   Then give a spoken-style briefing under 100 words: meetings, reminders, who is waiting on a
   reply (texts, Slack, email), which agents need the user, and the single most important
   thing. No lists of every item.
10. Chains across apps (e.g. "what did Taylor ask me? Draft a reply and remind me tomorrow if
   she doesn't answer"): do the steps in order. Read first, then show the exact draft and ask
   "Send it?" (Safety). Only after the user says yes, send it and then do the follow-ups, such
   as add_reminder naming the person. If they say no, send nothing and skip the follow-ups
   unless they still want them. A draft must not claim facts about the user's life you
   haven't read or been told (how someone is doing, plans, feelings); keep it short and
   neutral, or ask the user what to say.
"""


class HelperJob(BaseModel):
    app: str
    task: str


def _label(app: str, task: str) -> str:
    return " ".join(f"{app}: {task}".split()[:6])


async def _helper(hid: str, i: int, job: HelperJob, sid: str, write: bool) -> str:
    emit = EMIT.get()
    agent_id = (re.sub(r"[^A-Za-z0-9_-]", "", job.app)[:40] or "app") + f"-{i + 1}"
    dt._ctx.set({"snap": "", "session": sid, "agent": agent_id} if sid else {"snap": ""})
    if sid:
        await dt._ad("cursor-overlay", "enable", "--label", _label(job.app, job.task),
                     "--accent", COLORS[i % len(COLORS)])
    emit(type="helper", id=hid, app=job.app, state="start", text=job.task)
    agent = Agent(name=f"{job.app} helper", model=settings.agent_model("work"),
                  tools=[t for t in (*dt.ALL_TOOLS, *bt.WEB_TOOLS, *cw.CLOUD_TOOLS) if t.name in (UNDOABLE_TOOLS if write else READ_TOOLS)],
                  instructions=dt.INSTRUCTIONS + HELPER_RULES.format(
                      app=job.app, can=CAN_UNDOABLE if write else CAN_READ) + rules.block())
    result = Runner.run_streamed(agent, job.task, max_turns=25,
                                 run_config=RunConfig(tracing_disabled=settings.is_local("work")))
    try:
        async for ev in result.stream_events():
            if ev.type == "run_item_stream_event" and isinstance(ev.item, ToolCallItem):
                raw = ev.item.raw_item
                emit(type="helper", id=hid, app=job.app, state="tool",
                     name=getattr(raw, "name", "tool"), args=getattr(raw, "arguments", ""))
    except BaseException:
        result.cancel()
        raise
    text = str(result.final_output)
    emit(type="helper", id=hid, app=job.app, state="done", text=text)
    return text


@function_tool
async def run_helpers(jobs: list[HelperJob]) -> str:
    """Run several jobs at the same time, one helper per app, in the background. Helpers read and
    can make changes that are easy to undo (drafts, forms, notes, reminders, events), but never
    send, post, submit or delete. app: the exact app name; task: what to do there, in plain words.
    Returns every helper's findings."""
    return await run_jobs(jobs, write=True)


@function_tool(name_override="run_helpers")
async def run_read_helpers(jobs: list[HelperJob]) -> str:
    """Run several read-only jobs at the same time, one helper per app, in the background.
    app: the exact app name; task: what to find there, in plain words. Returns every helper's findings."""
    return await run_jobs(jobs)


async def run_jobs(jobs: list[HelperJob], write: bool = False) -> str:
    emit = EMIT.get()
    slots = asyncio.Semaphore(settings.get("helpers"))  # more jobs than slots: the rest wait their turn
    sid = ""
    if os.environ.get("MC_HELPER_CURSORS") == "1":
        d = await dt._ad("session", "start", "--name", "mac-copilot", "--cursor", "--multi-agent",
                         "--no-trace")
        sid = (d.get("data") or {}).get("session_id", "") if d.get("ok") else ""
    ids = [f"h{i + 1}" for i in range(len(jobs))]

    async def one(h: str, i: int, j: HelperJob) -> str:
        async with slots:
            return await asyncio.wait_for(_helper(h, i, j, sid, write), HELPER_TIMEOUT_S)
    try:
        results = await asyncio.gather(
            *(one(h, i, j) for i, (h, j) in enumerate(zip(ids, jobs))),
            return_exceptions=True)
    finally:
        if sid:
            await dt._ad("session", "end", sid)
    out = []
    for h, j, r in zip(ids, jobs, results):
        if isinstance(r, BaseException):
            msg = "didn't finish in time" if isinstance(r, TimeoutError) else f"failed: {str(r)[:200]}"
            emit(type="helper", id=h, app=j.app, state="error", text=msg)
            r = f"(helper {msg})"
        out.append(f"## {j.app}\n{r}")
    return "\n\n".join(out)


@function_tool
def show_draft(channel: str, to: str, message: str, subject: str = "") -> str:
    """Show the user the exact message you're about to send, before asking "Send it?". Call it once per
    message (text, email, Slack or a message to an agent). channel: text, email, slack or agent."""
    import decisions
    if not to.strip():
        return "Not shown: `to` is empty. Pass the recipient as the user named them (\"me\" for themselves)."
    msg = {"channel": channel, "to": to, "subject": subject, "message": message}
    decisions.propose(**msg)
    EMIT.get()(type="draft", **msg)
    return "Shown. Now ask \"Send it?\" and wait for the answer."


MAIN_TOOLS = [*dt.ALL_TOOLS, *bt.WEB_TOOLS, *cw.CLOUD_TOOLS, *jt.JARVIS_TOOLS, *ww.WATCH_TOOLS, *cf.FORM_TOOLS, *pt.PHONE_TOOLS, *pt.PHONE_WRITE, run_helpers, show_draft]

# Requests Jev classed as read-only run with reading tools only, so a wrong guess can't change anything.
NEEDS_MORE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("needs_more", default=None)


@function_tool
async def needs_more_access(reason: str) -> str:
    """Call this if the request needs anything beyond reading (typing, creating, changing, sending).
    reason: one short line."""
    flag = NEEDS_MORE.get()
    if flag is not None:
        flag["reason"] = reason
    return "OK. Stop now and reply with one short line saying you're handing it over."


READ_TOOLS_LIST = [*(t for t in (*dt.ALL_TOOLS, *bt.WEB_TOOLS, *cw.CLOUD_TOOLS, *ww.WATCH_TOOLS) if t.name in READ_TOOLS), *jt.JARVIS_READ, *pt.PHONE_TOOLS, run_read_helpers, needs_more_access]
# No tool may hang a request: anything slower than this ends with an error the model reports.
TOOL_TIMEOUT = 60
_SYNC = getattr(__import__("agents.tool", fromlist=["x"]), "_SYNC_FUNCTION_TOOL_MARKER", None)
for _t in {id(t): t for t in (*MAIN_TOOLS, *READ_TOOLS_LIST)}.values():
    if (getattr(_t, "timeout_seconds", 0) is None and _t.name not in ("run_helpers", "run_read_helpers")
            and not (_SYNC and getattr(_t.on_invoke_tool, _SYNC, False))):
        _t.timeout_seconds = TOOL_TIMEOUT
READ_RULE = """
This request was judged read-only, so you only have reading tools. If it actually needs you to
type, create, change, send or do anything beyond reading, call needs_more_access and stop.
"""

DEMO_NOTE = ("\n\n(Demo mode: an audience can see this screen. Refer to people by first name only, "
             "don't quote private messages word for word, and keep private details short.)")


def stamp(text: str) -> str:
    """Adds the current time to a request; without it the models guess the time (or say 11:15 am)."""
    return f"{text}\n\n(Now: {datetime.datetime.now():%A %-d %B %Y, %-I:%M %p})"


def demo_note(text: str) -> str:
    """Adds the demo-mode privacy note to a request while demo mode is on (MC_DEMO=1)."""
    return text + DEMO_NOTE if os.environ.get("MC_DEMO") == "1" else text
MAIN_INSTRUCTIONS = dt.INSTRUCTIONS.replace("\nSafety:", PARALLEL_RULE + cw.CLOUD_RULE + cf.FORM_RULE + bt.WEB_RULE + jt.JARVIS_RULE + pt.PHONE_RULE + "\nSafety:", 1)
MAIN_INSTRUCTIONS += ("\nDrafts: before sending a text, email, Slack message or agent message, call show_draft with the "
                      "exact recipient and wording, then ask \"Send it?\". If the user answers with an edited version, "
                      "send exactly their text. If the request itself says not to ask (\"just send it\", \"don't ask\"), "
                      "that is the user's yes: send it right away, without show_draft or \"Send it?\".\n")
READ_INSTRUCTIONS = MAIN_INSTRUCTIONS + READ_RULE

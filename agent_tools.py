"""The user's AI agents (Grok bots, Codex threads, Devin sessions) through Mission Control.

Mission Control (http://localhost:4777) already tracks all three and can deliver replies.
"""

import asyncio
import json
import time
import urllib.error
import urllib.request

from agents import function_tool

import decisions

MC = "http://localhost:4777"
PROVIDERS = {"grok": "Grok", "codex": "Codex", "devin": "Devin"}
ATTENTION = ("needs_you", "running", "stale", "waiting")
NOT_RUNNING = "Mission Control isn't running (http://localhost:4777), so I can't see your agents right now."


def _get_tasks() -> list[dict]:
    with urllib.request.urlopen(MC + "/api/tasks", timeout=15) as r:
        doc = json.load(r)
    return [t for t in doc.get("tasks", []) if t.get("provider") in PROVIDERS and not t.get("dismissed")]


def _ago(ms) -> str:
    mins = int((time.time() * 1000 - float(ms or 0)) / 60000)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 48 * 60:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def _clip(s, n) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _row(t: dict) -> str:
    bits = [f"- [{t['status']}] {PROVIDERS[t['provider']]}: {_clip(t.get('goal'), 90)}",
            f"agent={_clip(t.get('agentNote') or t.get('agent'), 40)}", f"last {_ago(t.get('lastActivityAt'))}"]
    if t.get("note"):
        bits.append(f"note: {_clip(t['note'], 140)}")
    if t.get("reply"):
        bits.append(f"latest: {_clip(t['reply'], 160)}")
    if t.get("pr"):
        bits.append(f"PR {t['pr']}")
    return " | ".join(bits) + f" | id={t['id']}"


def _status(provider: str, include_done: bool) -> str:
    try:
        tasks = _get_tasks()
    except (urllib.error.URLError, OSError):
        return NOT_RUNNING
    p = provider.lower().strip()
    if p in PROVIDERS:
        tasks = [t for t in tasks if t["provider"] == p]
    tasks.sort(key=lambda t: -float(t.get("lastActivityAt") or 0))
    live = [t for t in tasks if t.get("status") in ATTENTION]
    done = [t for t in tasks if t.get("status") not in ATTENTION]
    counts = ", ".join(
        f"{name}: " + (", ".join(f"{sum(1 for t in tasks if t['provider'] == k and t['status'] == s)} {s}"
                                 for s in ATTENTION + ("done",)
                                 if any(t['provider'] == k and t['status'] == s for t in tasks)) or "none")
        for k, name in PROVIDERS.items() if p not in PROVIDERS or k == p)
    order = {"needs_you": 0, "stale": 1, "waiting": 2, "running": 3}
    live.sort(key=lambda t: order.get(t["status"], 9))
    out = [f"Agents ({counts}).", "Needs attention or in progress:" if live else "Nothing needs you or is running."]
    out += [_row(t) for t in live]
    if include_done and done:
        out += ["Recently finished:"] + [_row(t) for t in done[:10]]
    return "\n".join(out)


def _find(tasks: list[dict], key: str) -> list[dict]:
    k = key.strip().lower()
    exact = [t for t in tasks if t["id"].lower() in (k, "devin:" + k, "grok:" + k, "codex:" + k)]
    if exact:
        return exact
    words = k.split()
    return [t for t in tasks
            if all(w in " ".join(str(t.get(f) or "") for f in ("goal", "agent", "agentNote", "project", "provider")).lower()
                   for w in words)]


def _thread(task: str, limit: int) -> str:
    try:
        tasks = _get_tasks()
    except (urllib.error.URLError, OSError):
        return NOT_RUNNING
    hits = sorted(_find(tasks, task), key=lambda t: -float(t.get("lastActivityAt") or 0))
    if not hits:
        return f"No agent task matches {task!r}. Use agent_status to see them all."
    if len(hits) > 1 and not any(t["id"] == task for t in hits):
        return "Several match; pick one by id:\n" + "\n".join(_row(t) for t in hits[:8])
    t = hits[0]
    msgs = (t.get("messages") or [])[-limit:]
    lines = [f"{'User' if m.get('role') == 'user' else PROVIDERS[t['provider']]}: {_clip(m.get('text'), 600)}" for m in msgs]
    return _row(t) + "\n\nLatest messages, oldest first:\n" + ("\n".join(lines) or "(no messages)")


def _message(task_id: str, message: str) -> str:
    req = urllib.request.Request(MC + "/api/send", data=json.dumps({"id": task_id, "text": message}).encode(),
                                 headers={"Content-Type": "application/json", "X-Mission-Control": "1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.load(r)
    except (urllib.error.URLError, OSError):
        return NOT_RUNNING
    if res.get("ok"):
        return f"Message delivered to {task_id}."
    return f"Couldn't deliver it: {res.get('error') or res}"


@function_tool
async def agent_status(provider: str = "all", include_done: bool = False) -> str:
    """Check the user's AI agents: Grok bots, Codex threads and Devin sessions. Shows which need the user,
    which are running, and each one's latest update. provider: 'all', 'grok', 'codex' or 'devin'."""
    return await asyncio.to_thread(_status, provider, include_done)


@function_tool
async def agent_thread(task: str, limit: int = 8) -> str:
    """Read the latest messages in one agent's thread. `task` is an id from agent_status or words from its goal/name."""
    return await asyncio.to_thread(_thread, task, max(1, min(limit, 30)))


@function_tool
async def message_agent(task_id: str, message: str) -> str:
    """Send a message to an agent thread (Grok bot, Codex thread or Devin session) by its id from agent_status.
    Grok briefly comes to the front to send. Only call this after the user has confirmed the exact wording."""
    if why := decisions.gate("agent", task_id, message):
        return why
    r = await asyncio.to_thread(_message, task_id, message)
    decisions.sent("agent", task_id, message, r)
    return r


AGENT_TOOLS = [agent_status, agent_thread, message_agent]

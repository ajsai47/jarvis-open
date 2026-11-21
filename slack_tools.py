"""Slack as the user, through the Slack Web API (no Slack window needed).

Uses SLACK_USER_TOKEN from this checkout’s .env, so messages are
sent as the user. Names come from Mission Control's Slack roster when it's there.
"""

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from functools import cache
from pathlib import Path

from agents import function_tool

import decisions
import people

ROSTER = os.environ.get("JARVIS_SLACK_ROSTER")
NO_TOKEN = "Slack isn't connected: add SLACK_USER_TOKEN to mac-copilot/.env."


class SlackError(Exception):
    pass


def _api(method: str, **params) -> dict:
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        raise SlackError(NO_TOKEN)
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}).encode()
    req = urllib.request.Request("https://slack.com/api/" + method, data=data,
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.load(r)
    if not doc.get("ok"):
        raise SlackError(f"Slack {method}: {doc.get('error')}")
    return doc


def _safe(fn, *args) -> str:
    try:
        return fn(*args)
    except SlackError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"Slack failed: {str(e)[:200]}"


@cache
def _me() -> tuple[str, str]:
    a = _api("auth.test")
    return a["user_id"], a.get("url", "")


@cache
def _roster() -> list[dict]:
    if not ROSTER:
        return []
    try:
        return [p for p in json.loads(Path(ROSTER).expanduser().read_text()) if p.get("slackId")]
    except (OSError, ValueError):
        return []


_names: dict[str, str] = {}


def _user_name(uid: str) -> str:
    if not uid:
        return "?"
    if uid == _me()[0]:
        return "you"
    if uid not in _names:
        hit = next((p for p in _roster() if p["slackId"] == uid), None)
        if hit:
            _names[uid] = hit.get("name") or hit.get("display") or uid
        else:
            try:
                u = _api("users.info", user=uid)["user"]
                _names[uid] = u.get("real_name") or u.get("name") or uid
            except Exception:  # noqa: BLE001
                _names[uid] = uid
    return _names[uid]


_channels: dict = {"at": 0, "list": []}


def _my_channels() -> list[dict]:
    if time.time() - _channels["at"] > 600:
        out, cursor = [], ""
        for _ in range(10):
            d = _api("users.conversations", types="public_channel,private_channel", limit=200,
                     exclude_archived="true", cursor=cursor)
            out += d.get("channels", [])
            cursor = d.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        _channels.update(at=time.time(), list=out)
    return _channels["list"]


def _find_person(name: str) -> list[dict]:
    n = name.strip().lstrip("@").lower()
    if n in ("me", "myself", "you", "self"):
        return [{"slackId": _me()[0], "name": "you (your own DM)"}]
    people = _roster()
    if not people:
        d = _api("users.list", limit=1000)
        people = [{"slackId": u["id"], "name": u.get("real_name") or u["name"],
                   "display": u.get("profile", {}).get("display_name", ""), "slackHandle": u["name"]}
                  for u in d.get("members", []) if not u.get("deleted") and not u.get("is_bot")]

    def keys(p):
        return [str(p.get(k) or "").lower() for k in ("name", "display", "slackHandle")]

    exact = [p for p in people if n in keys(p)]
    if exact:
        return exact
    return [p for p in people if any(k.startswith(n) for k in keys(p))
            or any(w.startswith(n) for w in keys(p)[0].split())][:8]


def _resolve(to: str) -> tuple[str, str]:
    """Return (channel_id, label) for '#channel', a person's name, or a raw Slack id."""
    t = to.strip()
    if t[:1] in "CGD" and t[1:].isalnum() and t.isupper() and len(t) >= 9:
        return t, t
    if t.lstrip("@")[:1] in "UW" and t.lstrip("@")[1:].isalnum() and t.lstrip("@").isupper() and len(t.lstrip("@")) >= 9:
        return _api("conversations.open", users=t.lstrip("@"))["channel"]["id"], f"DM with {t.lstrip('@')}"
    if not t.startswith("#") and (s := people.handle(t, "slack")):
        if s[:1] in "UW" and s[1:].isalnum() and s.isupper():
            return _api("conversations.open", users=s)["channel"]["id"], f"DM with {people.find(t)[0]['name']}"
        t = s
    if t.startswith("#") or not _find_person(t):
        name = t.lstrip("#").lower()
        ch = next((c for c in _my_channels() if c["name"].lower() == name), None)
        if ch:
            return ch["id"], "#" + ch["name"]
        if t.startswith("#"):
            raise SlackError(f"No channel called {t} that you're in.")
    found = _find_person(t)
    if not found:
        raise SlackError(f"Couldn't find anyone in Slack called {to!r}.")
    if len(found) > 1:
        names = "; ".join(f"{p.get('name')} (@{p.get('slackHandle', '')}) id {p['slackId']}" for p in found[:8])
        raise SlackError(f"More than one match for {to!r}: {names}. Ask the user which one, then pass the id.")
    p = found[0]
    ch = _api("conversations.open", users=p["slackId"])["channel"]["id"]
    return ch, f"DM with {p.get('name')}"


def _when(ts: str) -> str:
    d = datetime.fromtimestamp(float(ts))
    today = datetime.now().date()
    if d.date() == today:
        return d.strftime("today %-I:%M %p")
    return d.strftime("%a %b %-d %-I:%M %p")


def _where(ch: dict) -> str:
    if ch.get("is_im"):
        return "DM"
    if ch.get("is_mpim"):
        return "group DM"
    return "#" + (ch.get("name") or "?")


def _line(m: dict, where: str = "") -> str:
    who = _user_name(m.get("user", "")) if m.get("user") else (m.get("username") or "?")
    text = " ".join((m.get("text") or "").split())[:300]
    thread = f" [thread ts={m['thread_ts']}]" if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else ""
    return f"- {_when(m['ts'])} {where} {who}: {text}{thread} (ts={m['ts']})".replace("  ", " ")


def _search(query: str, count: int) -> list[dict]:
    d = _api("search.messages", query=query, count=count, sort="timestamp")
    return d.get("messages", {}).get("matches", [])


def _inbox(days: int) -> str:
    me = _me()[0]
    after = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
    seen, rows = set(), []
    for q in (f"to:me after:{after}", f"<@{me}> after:{after}"):
        for m in _search(q, 40):
            if m["ts"] in seen or m.get("user") == me:
                continue
            seen.add(m["ts"])
            ch = m.get("channel", {})
            label = _where(ch)
            if ch.get("is_im") or ch.get("is_mpim"):
                label += f" (channel {ch.get('id')})"
            rows.append((float(m["ts"]), _line(m, label)))
    if not rows:
        return f"No Slack DMs or mentions for you in the last {days} day(s)."
    rows.sort(reverse=True)
    return (f"Slack DMs and mentions to you, last {days} day(s), newest first "
            f"(not necessarily unanswered; read the conversation to check):\n" + "\n".join(r for _, r in rows[:40]))


def _read(conversation: str, limit: int, thread_ts: str) -> str:
    ch, label = _resolve(conversation)
    if thread_ts:
        msgs = _api("conversations.replies", channel=ch, ts=thread_ts, limit=limit)["messages"]
    else:
        msgs = list(reversed(_api("conversations.history", channel=ch, limit=limit)["messages"]))
    if not msgs:
        return f"No messages in {label}."
    return f"{label} (channel {ch}), oldest first:\n" + "\n".join(_line(m) for m in msgs)


def _do_search(query: str, limit: int) -> str:
    ms = _search(query, limit)
    if not ms:
        return f"No Slack messages match {query!r}."
    return f"Slack messages matching {query!r}, newest first:\n" + "\n".join(
        _line(m, _where(m.get("channel", {}))) for m in ms)


def _send(to: str, message: str, thread_ts: str) -> str:
    ch, label = _resolve(to)
    sent = _api("chat.postMessage", channel=ch, text=message, thread_ts=thread_ts)
    link = ""
    try:
        link = _api("chat.getPermalink", channel=ch, message_ts=sent["ts"])["permalink"]
    except Exception:  # noqa: BLE001
        pass
    return f"Sent to {label}{' (in thread)' if thread_ts else ''}. {link}".strip()


@function_tool
async def slack_inbox(days: int = 2) -> str:
    """Slack DMs, group DMs and @-mentions sent to the user in the last `days` days, newest first."""
    return await asyncio.to_thread(_safe, _inbox, max(1, min(days, 14)))


@function_tool
async def slack_read(conversation: str, limit: int = 20, thread_ts: str = "") -> str:
    """Read recent messages in a Slack channel ('#general'), a DM (a person's name) or a channel id.
    Pass thread_ts to read one thread's replies."""
    return await asyncio.to_thread(_safe, _read, conversation, max(1, min(limit, 100)), thread_ts)


@function_tool
async def slack_search(query: str, limit: int = 15) -> str:
    """Search Slack messages. Supports Slack search syntax, e.g. 'from:@pierce budget', 'in:#general after:2026-11-01'."""
    return await asyncio.to_thread(_safe, _do_search, query, max(1, min(limit, 50)))


@function_tool
async def slack_send(to: str, message: str, thread_ts: str | None = "") -> str:
    """Send a Slack message as the user: to a person (DM, by name), '#channel', 'me' or a channel id.
    Pass thread_ts to reply in a thread. Only call this after the user has confirmed the exact
    recipient and wording."""
    if why := decisions.gate("slack", to, message):
        return why
    r = await asyncio.to_thread(_safe, _send, to, message, thread_ts or "")
    decisions.sent("slack", to, message, r, thread=thread_ts)
    return r


SLACK_TOOLS = [slack_inbox, slack_read, slack_search, slack_send]

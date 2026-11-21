"""Start new Devin or Codex work (through Mission Control's own launcher), and the daily briefing."""
import asyncio
from datetime import datetime
import importlib.util
import os
import time
from pathlib import Path

from agents import function_tool

import briefing
import checkins
import decisions
import people
import rules

MC_FILE = Path.home() / "mission-control" / "mission_control.py"
_mc = None


def _launch(name: str, task: str, folder: str) -> dict:
    global _mc
    if _mc is None:
        spec = importlib.util.spec_from_file_location("mission_control", MC_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mc = mod
    tag = format(int(time.time() * 1000) & 0xFFFFFFFFFFFF, "012x")
    if name == "Codex":
        binary = _mc.CODEX_BIN if os.path.exists(_mc.CODEX_BIN) else _mc.shutil.which("codex")
        if not binary:
            return {"ok": False, "error": "Codex isn't installed"}
        return _mc._codex_app_run(binary, task, folder or str(Path.home()), tag)
    return _mc.launch_agent(name, {}, tag, task)


@function_tool
async def start_agent(agent: str, task: str, folder: str = "") -> str:
    """Start NEW work on one of the user's agents: agent "devin" (a new Devin session in the cloud) or
    "codex" (a new Codex thread on this Mac). task: the full brief, written so the agent can work
    without asking. folder: Codex only, the project folder (default: home folder). This starts real
    work: first show the user the agent and the exact brief and ask "Start it?"; only call it after
    their yes. To talk to an existing thread use message_agent instead."""
    name = {"devin": "Devin", "codex": "Codex"}.get(agent.strip().lower())
    if not name:
        return "agent must be devin or codex."
    if not task.strip():
        return "Write the brief first."
    if folder:
        folder = os.path.expanduser(folder)
        if not os.path.isdir(folder):
            return f"There's no folder {folder}."
    if not MC_FILE.exists():
        return "Mission Control isn't installed on this Mac, so I can't start agents."
    if why := decisions.gate("agent", name, task.strip()):
        return why
    try:
        r = await asyncio.to_thread(_launch, name, task.strip(), folder)
    except Exception as e:
        return f"Couldn't start {name}: {str(e)[:200]}"
    decisions.sent("agent", f"new {name}", task.strip(), r.get("url") or r.get("ref") or r.get("error") or "", folder=folder)
    if not r.get("ok"):
        return f"{name} didn't start: {r.get('error')}"
    if name == "Devin":
        return f"Started a Devin session: {r.get('url') or r.get('ref')}"
    return f"Started a Codex thread ({r.get('ref')}). It's open in the Codex app."


@function_tool
async def briefing_schedule(time_of_day: str = "", turn_on: str = "") -> str:
    """See or change the daily briefing: a "Brief me" Jarvis runs by itself once a day, shown as a card.
    time_of_day: 24-hour "HH:MM", e.g. "07:30". turn_on: "yes" or "no". Leave both empty to check it."""
    return briefing.change(time_of_day, turn_on)


@function_tool
async def briefing_status() -> str:
    """Check when the daily briefing runs (or whether it's off)."""
    return briefing.change()


HOOKS: dict = {}  # set by copilot_web: "answer" (verdict, about) -> str, "open" () -> [check-in texts]


@function_tool
async def remind_me(what: str, when: str) -> str:
    """Set a Jarvis reminder: at that time Jarvis pings the user (Mac notification + a card).
    what: e.g. "call Casey". when: "15:00", "3pm", "tomorrow 9:00", "YYYY-MM-DD HH:MM" or "in 20 minutes".
    Use this for "remind me…"; use add_reminder only when the user asks for the Reminders app."""
    return checkins.add_reminder(what, when)


@function_tool
async def reminders_and_checkins() -> str:
    """Upcoming Jarvis reminders (with ids) and check-ins waiting for the user's answer."""
    rows = [f"- #{r['id']} {datetime.fromtimestamp(r['at']):%a %H:%M}: {r['what']}" for r in checkins.upcoming()]
    open_ = HOOKS["open"]() if "open" in HOOKS else []
    out = ["Upcoming reminders:", *rows] if rows else ["No upcoming reminders."]
    out += ["Waiting for an answer (keep, snooze or drop):", *(f"- {x}" for x in open_)] if open_ else []
    return "\n".join(out)


@function_tool
async def cancel_reminder(reminder_id: int) -> str:
    """Cancel an upcoming Jarvis reminder by its id from reminders_and_checkins."""
    return "Cancelled." if checkins.cancel_reminder(reminder_id) else "No upcoming reminder has that id."


@function_tool
async def whats_slipping() -> str:
    """Work that has gone quiet in Mission Control: the user's own follow-ups, and agents waiting on
    the user or stalled, most urgent first, with how long each has been quiet. Use for "what's slipping?"."""
    try:
        rows = await asyncio.to_thread(checkins.slipping, None, None, True)
    except Exception:
        return "Mission Control isn't running, so I can't see your tasks."
    hours = checkins.load()["settings"]["stale_hours"]
    return (f"{len(rows)} item(s) quiet for {hours}+ hours:\n" + "\n".join(f"- {r['text']}" for r in rows[:15])
            if rows else f"Nothing has been quiet for more than {hours} hours.")


@function_tool
async def answer_checkin(answer: str, about: str = "") -> str:
    """Answer an open Jarvis check-in or reminder for the user: answer is "keep", "snooze" or "drop".
    about: words from that check-in, needed when several are open."""
    return HOOKS["answer"](answer, about) if "answer" in HOOKS else "Check-ins aren't running."


CLAUDE_MEM_SEARCH = "http://127.0.0.1:37701/api/search"


def _mem(query: str) -> str:
    import json
    import urllib.parse
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CLAUDE_MEM_SEARCH}?{urllib.parse.urlencode({'query': query, 'limit': 5})}",
                                    timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
    except OSError:
        return "(Claude Mem isn't running)"
    try:
        d = json.loads(body)
        body = "\n".join(c.get("text", "") for c in d.get("content", [])) if isinstance(d, dict) else body
    except ValueError:
        pass
    return " ".join(body.split())[:1500] or "(nothing)"


def _person_context(p: dict) -> str:
    import gmail_tools
    import slack_tools
    who = p["name"] or p["email"]
    parts = [f"## {who}" + (f" <{p['email']}>" if p["email"] and p["name"] else "")]
    if p["email"]:
        parts.append("Email: " + gmail_tools._safe(gmail_tools._list, f"from:{p['email']} OR to:{p['email']} newer_than:30d", 5))
    try:
        hits = slack_tools._find_person(p["name"]) if p["name"] else []
        q = f"from:<@{hits[0]['slackId']}>" if len(hits) == 1 else (p["name"] or p["email"])
        parts.append("Slack: " + slack_tools._safe(slack_tools._do_search, q, 5))
    except Exception as e:
        parts.append(f"Slack: couldn't search ({str(e)[:80]})")
    parts.append("Claude Mem: " + _mem(who))
    return "\n".join(parts)


@function_tool
async def prep_meeting(which: str = "") -> str:
    """Everything for a meeting brief: the meeting now or next on the calendar (or the next one whose title has
    the words in `which`), its attendees, recent email and Slack with each of them, and Claude Mem notes.
    Then answer with a brief the user can read in about a minute (under 150 words): when and who, why it
    matters, latest with each person, open threads or asks, and 1-3 things to raise. Read-only."""
    import calendar_tools
    m = await asyncio.to_thread(calendar_tools.next_meeting, which)
    note = ("\nCouldn't read: " + "; ".join(m["problems"])) if m and m.get("problems") else ""
    if not m or "title" not in m:
        return f"No upcoming meeting{' matching ' + repr(which) if which else ''} in the next 7 days." + note
    people = m["people"][:6]
    days = (m["start"].date() - datetime.now().date()).days
    rel = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"
    when = f"{rel}, {m['start']:%a %-d %b %H:%M}–{m['end']:%H:%M} (now: {datetime.now():%a %-d %b %H:%M})"
    head = (f"Meeting: {m['title']} · {when}" + (f" @ {m['where']}" if m["where"] else "")
            + f" [{m['cal']}]\nAttendees: " + (", ".join(p["name"] or p["email"] for p in m["people"]) or "none listed")
            + (f"\nInvite notes: {m['notes']}" if m["notes"] else ""))
    ctx = await asyncio.gather(*(asyncio.to_thread(_person_context, p) for p in people),
                               asyncio.to_thread(_mem, m["title"]))
    return "\n\n".join([head, *ctx[:-1], "## About the meeting\nClaude Mem: " + ctx[-1]]) + note


@function_tool
async def list_rules() -> str:
    """the user’s standing rules for Jarvis, numbered (the same list as Settings → Rules)."""
    rs = rules.load()
    return "\n".join(f"{i}. {r}" for i, r in enumerate(rs, 1)) if rs else "No rules yet."


@function_tool
async def save_rule(rule: str, number: int = 0) -> str:
    """Save one of the user’s standing rules. number 0 adds a new rule; a number from list_rules replaces that
    rule's wording. Only call this after the user said yes to the exact wording."""
    rs = rules.load()
    if not rule.strip():
        return "Write the rule first."
    if number:
        if not 1 <= number <= len(rs):
            return f"There's no rule {number}; there are {len(rs)}."
        rs[number - 1] = rule
    else:
        rs.append(rule)
    rs = rules.save(rs, "jarvis")
    return f"Saved. the user now has {len(rs)} rule(s); it applies from the next request:\n" + "\n".join(
        f"{i}. {r}" for i, r in enumerate(rs, 1))


@function_tool
async def remove_rule(number: int) -> str:
    """Delete rule `number` (from list_rules). Only after the user said yes to removing that exact rule."""
    rs = rules.load()
    if not 1 <= number <= len(rs):
        return f"There's no rule {number}; there are {len(rs)}."
    gone = rs.pop(number - 1)
    rules.save(rs, "jarvis")
    return f"Removed: {gone}"


def _person_line(p: dict) -> str:
    extra = [f"{k}: {p[k]}" for k in ("group", "phone", "email", "slack", "notes") if p.get(k)]
    return "- " + people._public(p) + (" | " + " | ".join(extra) if extra else "")


@function_tool
async def list_people(query: str = "") -> str:
    """the user’s people (the same list as Settings → People) with their phone, email, Slack and notes.
    `query` is a name, nickname or description ("Mom", "my cofounder"); empty lists everyone."""
    ps = people.find(query) if query.strip() else people.load()
    if not ps:
        return f"Nobody on file matches {query!r}." if query.strip() else "No people saved yet."
    return "\n".join(_person_line(p) for p in ps[:100])


@function_tool
async def save_person(name: str, who: str = "", nicknames: str = "", phone: str = "", email: str = "",
                      slack: str = "", notes: str = "", group: str = "") -> str:
    """Add someone to the user’s people, or update them (matched by name; only the fields given change, and
    nicknames are added to theirs). nicknames is comma-separated. group is "Work" (work) or "Life".
    Only after the user said yes to the exact details."""
    if not name.strip():
        return "Who? Give their name."
    ps = people.load()
    p = next((x for x in ps if x["name"].lower() == name.strip().lower()), None)
    if p is None:
        p = {"name": name}
        ps.append(p)
    for k, v in (("who", who), ("group", group), ("phone", phone), ("email", email), ("slack", slack), ("notes", notes)):
        if v.strip():
            p[k] = v
    if nicknames.strip():
        p["aka"] = [*p.get("aka", []), *(n for n in nicknames.split(",") if n.strip().lower() not in
                                         {a.lower() for a in p.get("aka", [])})]
    people.save(ps, "jarvis")
    saved = next((x for x in people.load() if x["name"].lower() == name.strip().lower()), p)
    return "Saved:\n" + _person_line(saved)


@function_tool
async def remove_person(name: str) -> str:
    """Remove someone from the user’s people, by their exact name. Only after the user said yes."""
    ps = people.load()
    keep = [p for p in ps if p["name"].lower() != name.strip().lower()]
    if len(keep) == len(ps):
        return f"Nobody called {name!r} is saved. Use list_people to see the names."
    people.save(keep, "jarvis")
    return f"Removed {name}."


JARVIS_TOOLS = [list_people, save_person, remove_person, list_rules, save_rule, remove_rule, prep_meeting, start_agent, briefing_schedule, briefing_status, remind_me, reminders_and_checkins, cancel_reminder,
                whats_slipping, answer_checkin]
JARVIS_READ = [list_people, list_rules, prep_meeting, briefing_status, reminders_and_checkins, whats_slipping]
JARVIS_RULE = """
the user’s rules: his standing preferences are listed at the very end of these instructions (and in
Settings → Rules). When he states a lasting preference ("new rule: …", "from now on…", "always/never…"),
word it as one short rule, say it back and ask "Save it?"; call save_rule only after his yes. "What are
my rules?" uses list_rules; to change or drop one, say which rule and the new wording, then after his
yes use save_rule with its number or remove_rule.
People: the user’s people (who "Mom", "Jess" or "my cofounder" is) are listed at the very end of these
instructions. Use them to know who he means; when several fit, ask which. When he tells you who someone
is ("Jess is my sister, her number is …"), or you had to ask which person he meant, say the details back
and ask "Save it?"; call save_person only after his yes. "Forget Jess" uses remove_person after his yes.
Reminders and check-ins: "remind me…" uses remind_me (Jarvis pings at that time). Jarvis also asks
on its own about work that has gone quiet ("Still need it?"); when the user answers keep, snooze or
drop, call answer_checkin. "What's slipping?" uses whats_slipping.
Meeting prep: "prep me for my next meeting" (or a named one) uses prep_meeting; reply with the
one-minute brief it asks for, not the raw notes.
New agent work: to start a new Devin session or Codex thread, use start_agent, only after the
user's yes on the agent and the exact brief (end with "Start it?"). The daily briefing time is set
with briefing_schedule.
"""

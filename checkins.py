"""Jarvis's reminders and check-ins (logs/checkins.json).

Reminders: "remind me at 3 to call Casey" pings at that time.
Check-ins: Jarvis asks on its own about work that has gone quiet in Mission Control ("You haven't
touched X in 6 hours. Still need it?"), answered keep / snooze / drop. They only go out when the user is
at the Mac, not in a meeting, outside quiet hours, under the daily cap and not too close together."""

import json
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

FILE = Path(__file__).resolve().parent / "logs" / "checkins.json"
MC = "http://localhost:4777"
DEFAULT = {"on": True, "cap": 6, "quiet_from": "21:00", "quiet_to": "08:00", "stale_hours": 6}
LIMITS = {"cap": (1, 30), "stale_hours": (1, 72)}
GAP = 30 * 60  # at least 30 min between check-ins
AWAY = 15 * 60  # no keyboard or mouse for 15 min: the user isn't at the Mac, so wait
TOO_OLD = 14 * 86400  # work quiet for 2 weeks isn't "slipping" any more
KEEP, SNOOZE, REMIND_SNOOZE = 86400, 3 * 3600, 3600
VERDICT = re.compile(r"^\s*(keep|snooze|drop)\b", re.I)
_lock = threading.RLock()


def load() -> dict:
    try:
        s = json.loads(FILE.read_text())
    except (OSError, ValueError):
        s = {}
    s["settings"] = {**DEFAULT, **s.get("settings", {})}
    s.setdefault("reminders", [])
    s.setdefault("answers", {})
    s.setdefault("sent", [])  # times check-ins went out (for the cap and the gap)
    return s


def save(s: dict) -> None:
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(FILE)


def _hhmm(v: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(v).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("Use HH:MM, e.g. 21:00.")
    return f"{int(m[1]):02d}:{m[2]}"


def change(**kw) -> dict:
    with _lock:
        s = load()
        st = s["settings"]
        if "on" in kw:
            st["on"] = bool(kw["on"])
        for k in ("quiet_from", "quiet_to"):
            if k in kw:
                st[k] = _hhmm(kw[k])
        for k, (lo, hi) in LIMITS.items():
            if k in kw:
                st[k] = max(lo, min(hi, int(kw[k])))
        save(s)
        return st


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """'15:00', '3pm', '3:30 pm', 'tomorrow 9:00', '2026-11-08 09:00', 'in 20 minutes', 'in 2 hours'."""
    now = now or datetime.now()
    w = when.strip().lower()
    m = re.fullmatch(r"in\s+(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)", w)
    if m:
        n = float(m[1])
        return now + (timedelta(hours=n) if m[2].startswith("h") else timedelta(minutes=n))
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ t](\d{1,2}):(\d{2})", w)
    if m:
        return datetime.fromisoformat(f"{m[1]} {int(m[2]):02d}:{m[3]}")
    day = 0
    if w.startswith("tomorrow"):
        day, w = 1, w[8:].strip(" ,at")
    w = w.removeprefix("at ").strip()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", w)
    if not m:
        return None
    h, mi = int(m[1]), int(m[2] or 0)
    if m[3] == "pm" and h < 12:
        h += 12
    if m[3] == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    at = now.replace(hour=h, minute=mi, second=0, microsecond=0) + timedelta(days=day)
    if not day and at <= now:
        if not m[3] and h < 12 and at + timedelta(hours=12) > now:
            at += timedelta(hours=12)  # "at 3" said at 1 pm means 3 pm
        else:
            at += timedelta(days=1)
    return at


def add_reminder(what: str, when: str) -> str:
    at = parse_when(when)
    if not at:
        return "I couldn't read that time. Say e.g. '15:00', '3pm', 'tomorrow 9:00' or 'in 20 minutes'."
    with _lock:
        s = load()
        rid = max((r["id"] for r in s["reminders"]), default=0) + 1
        s["reminders"].append({"id": rid, "what": what.strip(), "at": at.timestamp(), "state": "pending"})
        save(s)
    return f"OK, I'll remind you to {what.strip()} at {at:%H:%M}" + ("" if at.date() == datetime.now().date() else f" on {at:%a %-d %b}") + "."


def due_reminders(now: float | None = None) -> list[dict]:
    """Pending reminders whose time has come; marks them fired."""
    now = now or time.time()
    with _lock:
        s = load()
        due = [r for r in s["reminders"] if r["state"] == "pending" and r["at"] <= now]
        for r in due:
            r["state"] = "fired"
        if due:
            s["reminders"] = [r for r in s["reminders"] if r["state"] == "pending" or now - r["at"] < 7 * 86400]
            save(s)
        return due


def upcoming() -> list[dict]:
    return sorted((r for r in load()["reminders"] if r["state"] == "pending"), key=lambda r: r["at"])


def cancel_reminder(rid: int) -> bool:
    with _lock:
        s = load()
        hit = [r for r in s["reminders"] if r["id"] == rid and r["state"] == "pending"]
        for r in hit:
            r["state"] = "dropped"
        save(s)
        return bool(hit)


def _mc_tasks() -> list[dict]:
    req = urllib.request.Request(MC + "/api/tasks", headers={"X-Mission-Control": "1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    return d.get("tasks", []) if isinstance(d, dict) else d


def _hours(sec: float) -> str:
    h = sec / 3600
    return f"{round(h)} hours" if h < 48 else f"{round(h / 24)} days"


def slipping(tasks: list[dict] | None = None, now: float | None = None, all_: bool = False) -> list[dict]:
    """Mission Control work that has gone quiet: [{key, text, goal, agent, status, quiet}], most urgent first.
    all_ also includes work the user said to keep or snooze."""
    now = now or time.time()
    s = load()
    tasks = _mc_tasks() if tasks is None else tasks
    limit = s["settings"]["stale_hours"] * 3600
    out = []
    for t in tasks:
        if t.get("dismissed") or t.get("status") not in ("needs_you", "stale", "waiting"):
            continue
        quiet = now - float(t.get("lastActivityAt") or 0) / 1000
        if not limit <= quiet <= TOO_OLD:
            continue
        key = "mc:" + str(t["id"])
        a = s["answers"].get(key)
        if a and (a["verdict"] == "drop" or a.get("until", 0) > now) and not (all_ and a["verdict"] != "drop"):
            continue
        goal = (t.get("goal") or t.get("note") or "this").strip().rstrip(".")
        goal = goal if len(goal) <= 90 else goal[:87] + "…"
        who = t.get("agent") or t.get("project") or t.get("provider", "")
        if t.get("provider") == "person":
            text = f"You haven't touched \u201c{goal}\u201d in {_hours(quiet)}. Still need it?"
        elif t["status"] == "needs_you":
            text = f"{who} has been waiting on you for {_hours(quiet)} about \u201c{goal}\u201d. Still need it?"
        else:
            text = f"{who} hasn't moved on \u201c{goal}\u201d in {_hours(quiet)}. Still need it?"
        out.append({"key": key, "text": text, "goal": goal, "agent": who, "provider": t.get("provider", ""),
                    "status": t["status"], "quiet": quiet})
    rank = {"needs_you": 0, "waiting": 1, "stale": 2}
    out.sort(key=lambda x: (rank[x["status"]], x["quiet"]))
    return out


def idle_seconds() -> float:
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
        return int(re.search(r'"HIDIdleTime" = (\d+)', out)[1]) / 1e9
    except Exception:
        return 0.0


def _quiet(now: datetime, st: dict) -> bool:
    t, a, b = now.strftime("%H:%M"), st["quiet_from"], st["quiet_to"]
    return a <= t < b if a <= b else (t >= a or t < b)


def blocked(now: float | None = None, meeting: str = "", idle: float = 0.0) -> str:
    """Why a check-in can't go out right now ("" = it can)."""
    now = now or time.time()
    s = load()
    st = s["settings"]
    today = [x for x in s["sent"] if datetime.fromtimestamp(x).date() == datetime.fromtimestamp(now).date()]
    if not st["on"]:
        return "check-ins are off"
    if _quiet(datetime.fromtimestamp(now), st):
        return f"quiet hours ({st['quiet_from']}–{st['quiet_to']})"
    if len(today) >= st["cap"]:
        return f"today's limit of {st['cap']} check-ins is reached"
    if s["sent"] and now - max(s["sent"]) < GAP:
        return "the last check-in was under 30 minutes ago"
    if meeting:
        return f"you're in a meeting ({meeting})"
    if idle > AWAY:
        return "you're away from the Mac"
    return ""


def sent_today() -> int:
    today = datetime.now().date()
    return sum(datetime.fromtimestamp(x).date() == today for x in load()["sent"])


def mark_sent(now: float | None = None) -> None:
    now = now or time.time()
    with _lock:
        s = load()
        s["sent"] = [x for x in s["sent"] if now - x < 2 * 86400] + [now]
        save(s)


def answer(key: str, verdict: str, now: float | None = None) -> str:
    """keep / snooze / drop. Returns what Jarvis says back."""
    now = now or time.time()
    verdict = verdict.lower()
    if key.startswith("watch:"):
        import watch_web
        return watch_web.answer(int(key[6:]), verdict)
    with _lock:
        s = load()
        if key.startswith("rem:"):
            r = next((x for x in s["reminders"] if f"rem:{x['id']}" == key), None)
            if r and verdict == "snooze":
                r["at"], r["state"] = now + REMIND_SNOOZE, "pending"
                save(s)
                return f"Snoozed. I'll remind you again at {datetime.fromtimestamp(r['at']):%H:%M}."
            if r:
                r["state"] = "done" if verdict == "keep" else "dropped"
                save(s)
            return "Got it." if verdict == "keep" else "Dropped."
        until = {"keep": now + KEEP, "snooze": now + SNOOZE}.get(verdict, 0)
        s["answers"][key] = {"verdict": verdict, "at": now, "until": until}
        save(s)
    if verdict == "keep":
        return "Kept. I won't ask about it again today."
    if verdict == "snooze":
        return f"Snoozed. I'll check again after {datetime.fromtimestamp(until):%H:%M}."
    return "Dropped. I'll stop asking about it. It's still in Mission Control if you want to close it there."

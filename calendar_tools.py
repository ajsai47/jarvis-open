"""Calendar for Mac Copilot: read events straight from Calendar's database (read-only, no window) and from
Google Calendar, and add events to Google Calendar (API) or Apple Calendar (AppleScript), with no window.

Reading needs Full Disk Access for the app that starts Mac Copilot (the same as texts)."""

import asyncio
import datetime
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

from agents import function_tool

DB = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
GCAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _day(day: str) -> datetime.date | None:
    base = datetime.date.today()
    named = {"": base, "today": base, "tomorrow": base + datetime.timedelta(days=1),
             "yesterday": base - datetime.timedelta(days=1)}
    if day.lower() in named:
        return named[day.lower()]
    try:
        return datetime.date.fromisoformat(day)
    except ValueError:
        return None


def _ts(d: datetime.datetime) -> float:
    return (d.astimezone(datetime.timezone.utc) - EPOCH).total_seconds()


def _local(t: float) -> datetime.datetime:
    return (EPOCH + datetime.timedelta(seconds=t)).astimezone()


def _apple(lo: datetime.datetime, hi: datetime.datetime) -> list[dict]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = db.execute("""
            SELECT i.ROWID, COALESCE(o.occurrence_start_date, o.occurrence_date) AS st, o.occurrence_end_date, i.summary,
                   i.all_day, l.title, c.title
            FROM OccurrenceCache o
            JOIN CalendarItem i ON i.ROWID = o.event_id
            JOIN Calendar c ON c.ROWID = o.calendar_id
            LEFT JOIN Location l ON l.ROWID = i.location_id
            WHERE COALESCE(o.occurrence_start_date, o.occurrence_date) < ? AND o.occurrence_end_date > ?
              AND (i.status IS NULL OR i.status != 3)
            ORDER BY i.all_day DESC, st""", (_ts(hi), _ts(lo))).fetchall()
        out = []
        for rid, st, en, title, all_day, loc, cal in rows:
            people = []
            with suppress(sqlite3.Error):
                people = [{"name": "", "email": e.removeprefix("mailto:")} for (e,) in
                          db.execute("SELECT email FROM Participant WHERE owner_id = ? AND email IS NOT NULL", (rid,))]
            out.append({"title": title or "(no title)", "all_day": bool(all_day), "where": loc or "", "cal": cal,
                        "start": (EPOCH + datetime.timedelta(seconds=st)) if all_day else _local(st),
                        "end": _local(en or st), "people": people, "notes": ""})
        return out
    finally:
        db.close()


def _google_ok() -> bool:
    import gmail_tools
    try:
        return "calendar" in json.loads(gmail_tools.TOKEN_FILE.read_text()).get("scope", "")
    except (OSError, ValueError):
        return False


def google_can_add() -> bool:
    """True when the Google sign-in allows adding events (not just reading them)."""
    import gmail_tools
    try:
        scope = json.loads(gmail_tools.TOKEN_FILE.read_text()).get("scope", "").split()
    except (OSError, ValueError):
        return False
    return any(s.endswith(("/auth/calendar", "/auth/calendar.events")) for s in scope)


def _google(lo: datetime.datetime, hi: datetime.datetime) -> list[dict]:
    """Primary Google Calendar (the work calendar Notion Calendar shows), through the Gmail sign-in."""
    import gmail_tools
    q = urllib.parse.urlencode({"timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
                                "orderBy": "startTime", "maxResults": 100})
    req = urllib.request.Request(f"{GCAL}?{q}", headers={"Authorization": "Bearer " + gmail_tools._token()})
    with urllib.request.urlopen(req, timeout=15) as r:
        items = json.load(r).get("items", [])
    out = []
    for e in items:
        me = next((a for a in e.get("attendees", []) if a.get("self")), {})
        if e.get("status") == "cancelled" or me.get("responseStatus") == "declined":
            continue
        s, f = e.get("start", {}), e.get("end", {})
        all_day = "date" in s
        out.append({"title": e.get("summary") or "(no title)", "all_day": all_day, "where": e.get("location", ""),
                    "cal": "Google",
                    "start": datetime.datetime.fromisoformat(s.get("dateTime") or s["date"]).astimezone(),
                    "end": datetime.datetime.fromisoformat(f.get("dateTime") or f.get("date") or s["date"]).astimezone(),
                    "people": [{"name": a.get("displayName", ""), "email": a.get("email", "")}
                               for a in e.get("attendees", []) if not a.get("self") and not a.get("resource")],
                    "notes": " ".join((e.get("description") or "").split())[:600]})
    return out


def events(lo: datetime.datetime, hi: datetime.datetime) -> tuple[list[dict], list[str]]:
    """Apple Calendar plus Google Calendar between lo and hi, and what couldn't be read."""
    got, problems = [], []
    try:
        got += _apple(lo, hi)
    except sqlite3.Error:
        problems.append("Apple Calendar (needs Full Disk Access for the app that starts Jarvis)")
    if _google_ok():
        try:
            got += _google(lo, hi)
        except urllib.error.HTTPError as e:
            off = "has not been used" in e.read().decode("utf-8", "replace")
            problems.append("Google Calendar (turn on the Calendar API in Google Cloud for Jarvis's project)" if off
                            else f"Google Calendar (error {e.code})")
        except Exception as e:
            problems.append(f"Google Calendar ({str(e)[:120]})")
    else:
        problems.append("Google Calendar (not connected: double-click app/Connect Gmail.command and allow Calendar)")
    seen: dict = {}
    for e in sorted(got, key=lambda e: (not e["all_day"], e["start"])):
        k = (e["title"].lower(), e["start"].strftime("%Y%m%d%H%M"))
        if k not in seen:
            seen[k] = e
        elif len(e["people"]) > len(seen[k]["people"]):  # same event in both calendars: keep the attendees
            seen[k].update(people=e["people"], notes=seen[k]["notes"] or e["notes"])
    return list(seen.values()), problems


def _line(e: dict) -> str:
    when = f"{e['start']:%a %-d %b}, all day" if e["all_day"] else f"{e['start']:%a %-d %b %H:%M}–{e['end']:%H:%M}"
    who = f" with {', '.join(p['name'] or p['email'] for p in e['people'][:8])}" if e["people"] else ""
    return f"- {when}: {e['title']}" + (f" @ {e['where']}" if e["where"] else "") + who + f" [{e['cal']}]"


def busy_now() -> str:
    """The title of the timed event happening right now ("" if none). Raises if no calendar can be read."""
    now = datetime.datetime.now().astimezone()
    evs, problems = events(now, now + datetime.timedelta(seconds=1))
    if len(problems) == 2:
        raise RuntimeError("no calendar can be read")
    return next((e["title"] for e in evs if not e["all_day"] and e["start"] <= now < e["end"]), "")


def next_meeting(match: str = "", days: int = 7) -> dict | None:
    """The meeting happening now or next (or the next one whose title has `match`), with attendees."""
    now = datetime.datetime.now().astimezone()
    evs, problems = events(now, now + datetime.timedelta(days=days))
    words = match.lower().split()
    for e in evs:
        if not e["all_day"] and e["end"] > now and all(w in e["title"].lower() for w in words):
            return {**e, "problems": problems}
    return {"problems": problems} if problems else None


@function_tool
async def list_events(day: str = "today", days: int = 1) -> str:
    """List calendar events (Apple Calendar and Google Calendar), with attendees, without opening any window.
    day: "today", "tomorrow", "yesterday" or YYYY-MM-DD; days: how many days from there (1-14)."""
    d = _day(day)
    if not d:
        return "Give day as today, tomorrow or YYYY-MM-DD."
    days = max(1, min(days, 14))
    lo = datetime.datetime.combine(d, datetime.time()).astimezone()
    evs, problems = await asyncio.to_thread(events, lo, lo + datetime.timedelta(days=days))
    span = f"{d:%a %-d %b}" + (f" + {days - 1} days" if days > 1 else "")
    text = f"{len(evs)} event(s), {span}:\n" + "\n".join(map(_line, evs)) if evs else f"No events ({span})."
    return text + ("\nCouldn't read: " + "; ".join(problems) if problems else "")


ADD_TIMEOUT = 20
_ADD_EVENT = """
on run argv
    set t to item 1 of argv
    set d to current date
    set day of d to 1
    set year of d to (item 2 of argv) as integer
    set month of d to (item 3 of argv) as integer
    set day of d to (item 4 of argv) as integer
    set time of d to ((item 5 of argv) as integer) * 60
    set e to d + ((item 6 of argv) as integer) * 60
    set calName to item 7 of argv
    set allDay to (item 8 of argv) is "1"
    tell application "Calendar"
        if calName is "" then
            set cal to first calendar whose writable is true
        else
            set cal to first calendar whose name is calName
        end if
        if allDay then
            make new event at end of events of cal with properties {summary:t, start date:d, end date:e, allday event:true}
        else
            make new event at end of events of cal with properties {summary:t, start date:d, end date:e}
        end if
        return name of cal
    end tell
end run
"""


def _check(title: str, evs: list[dict]) -> bool:
    return any(e["title"].strip().lower() == title.strip().lower() for e in evs)


async def _google_add(title: str, d: datetime.date, h: int, m: int, minutes: int, all_day: bool) -> str:
    import gmail_tools
    if all_day:
        when = {"start": {"date": d.isoformat()}, "end": {"date": (d + datetime.timedelta(days=1)).isoformat()}}
    else:
        st = datetime.datetime.combine(d, datetime.time(h, m)).astimezone()
        when = {"start": {"dateTime": st.isoformat()},
                "end": {"dateTime": (st + datetime.timedelta(minutes=minutes)).isoformat()}}

    def post() -> dict:
        req = urllib.request.Request(GCAL, data=json.dumps({"summary": title, "transparency": "opaque", **when}).encode(),
                                     method="POST", headers={"Authorization": "Bearer " + gmail_tools._token(),
                                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=ADD_TIMEOUT) as r:
            return json.load(r)
    try:
        await asyncio.to_thread(post)
    except urllib.error.HTTPError as e:
        return (f"NOT added: Google Calendar refused it (error {e.code}: "
                f"{e.read().decode('utf-8', 'replace')[:160]}). Tell the user plainly that it wasn't added.")
    except Exception as e:
        return f"NOT added: couldn't reach Google Calendar ({str(e)[:160]}). Tell the user plainly that it wasn't added."
    span = "all day" if all_day else f"at {h:02d}:{m:02d} for {minutes} min"
    done = f"Added '{title}' on {d:%a %-d %b} {span} to Google Calendar (shows in Notion Calendar)"
    lo = datetime.datetime.combine(d, datetime.time()).astimezone()
    try:
        evs = await asyncio.to_thread(_google, lo, lo + datetime.timedelta(days=1))
    except Exception:
        return done + " (couldn't read it back to double-check)."
    return done + (", and checked: it's there." if _check(title, evs) else
                   ", but it doesn't show up yet when reading it back; it may still be syncing.")


@function_tool
async def add_event(title: str, day: str, start: str = "09:00", minutes: int = 30,
                    calendar: str = "", all_day: bool = False, calendar_app: str = "") -> str:
    """Add an event (no invitees) without opening any window.
    day: "today", "tomorrow" or YYYY-MM-DD; start: 24-hour HH:MM. all_day: true to block off the whole day
    (start and minutes are then ignored). calendar_app: "google" (Google Calendar, which Notion Calendar
    shows), "apple" (Apple Calendar), or "" for Google when it can add events, otherwise Apple.
    calendar: Apple only, a calendar name from list_events, or "" for the first one that can be edited."""
    d = _day(day)
    try:
        h, m = (0, 0) if all_day else (int(x) for x in start.split(":"))
    except ValueError:
        d = None
    if not d:
        return "Give day as today, tomorrow or YYYY-MM-DD and start as HH:MM."
    if all_day:
        minutes = 1440
    app = calendar_app.strip().lower() or ("google" if google_can_add() else "apple")
    if app.startswith("google") or app.startswith("notion"):
        if not google_can_add():
            return (f"NOT added: Jarvis's Google sign-in can only read the calendar, so '{title}' isn't on Google "
                    "Calendar. the user needs to reconnect Google once (double-click app/Connect Gmail and allow "
                    "Calendar). Tell him plainly that it wasn't added.")
        return await _google_add(title, d, h, m, minutes, all_day)
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _ADD_EVENT, title, str(d.year), str(d.month), str(d.day),
        str(h * 60 + m), str(max(5, minutes)), calendar, "1" if all_day else "0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), ADD_TIMEOUT)
    except TimeoutError:
        proc.kill()
        return (f"NOT added: Calendar didn't answer within {ADD_TIMEOUT} s, so '{title}' isn't on the calendar. "
                "Calendar may be stuck or showing a prompt on the Mac. Tell the user plainly that it wasn't added.")
    if proc.returncode:
        if b"-1743" in err:
            return ("macOS blocked it: allow Mac Copilot to control Calendar in System Settings → "
                    "Privacy & Security → Automation.")
        return f"Couldn't add it: {err.decode()[:200]}"
    when = "all day" if all_day else f"at {h:02d}:{m:02d} for {minutes} min"
    done = f"Added '{title}' on {d:%a %-d %b} {when} to calendar '{out.decode().strip()}'"
    lo = datetime.datetime.combine(d, datetime.time()).astimezone()
    try:
        evs, _ = await asyncio.to_thread(events, lo, lo + datetime.timedelta(days=1))
    except Exception:
        return done + " (couldn't read the calendar back to double-check)."
    if _check(title, evs):
        return done + ", and checked: it's there."
    return done + ", but it doesn't show up yet when reading the calendar back; it may still be syncing."


CALENDAR_TOOLS = [list_events, add_event]

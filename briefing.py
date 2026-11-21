"""The daily briefing schedule (logs/briefing.json): Jarvis runs "Brief me" by itself once a day."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

SCHEDULE = Path(__file__).resolve().parent / "logs" / "briefing.json"
DEFAULT = {"on": True, "time": "08:00", "last": ""}
LATE_LIMIT = timedelta(hours=2)  # Mac asleep at 8:00 and woken by 10:00: still brief; later: skip the day


def load() -> dict:
    try:
        return {**DEFAULT, **json.loads(SCHEDULE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT)


def save(s: dict) -> None:
    SCHEDULE.parent.mkdir(exist_ok=True)
    SCHEDULE.write_text(json.dumps(s))


def change(time_of_day: str = "", turn_on: str = "") -> str:
    s = load()
    if time_of_day:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_of_day.strip())
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return "Use 24-hour HH:MM, e.g. 07:30."
        s["time"], s["on"] = f"{int(m[1]):02d}:{m[2]}", True
        if s.get("last") == datetime.now().strftime("%Y-%m-%d") and datetime.now().strftime("%H:%M") < s["time"]:
            s["last"] = ""  # moved later today: still run today
    if turn_on:
        s["on"] = turn_on.strip().lower() in ("yes", "on", "true", "y")
    if time_of_day or turn_on:
        save(s)
    return f"Daily briefing: {'on, every day at ' + s['time'] if s['on'] else 'off'}."


def due(now: datetime | None = None) -> str:
    """The scheduled time ("08:00") if a briefing should start now (and marks today done), else ""."""
    now = now or datetime.now()
    s = load()
    today = now.strftime("%Y-%m-%d")
    if not s["on"] or s.get("last") == today:
        return ""
    h, m = map(int, s["time"].split(":"))
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start <= now < start + LATE_LIMIT:
        s["last"] = today
        save(s)
        return s["time"]
    return ""

"""Messages (texts and iMessage): read straight from its database on the Mac, send through the app.

Needs Full Disk Access for the app that starts Mac Copilot (Mac Copilot.app, or Terminal), and permission for it
to control Messages (macOS asks once). Reading never changes anything.
"""

import asyncio
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

from agents import function_tool

import decisions
import people

HOME = Path.home()
CHAT_DB = HOME / "Library/Messages/chat.db"
CONTACT_DBS = HOME / "Library/Application Support/AddressBook"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NO_ACCESS = ("Mac Copilot can't read Messages yet. In System Settings → Privacy & Security → "
             "Full Disk Access, turn on Mac Copilot (or Terminal if you start it from there), then restart it.")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _when(raw: int | None) -> datetime:
    raw = raw or 0
    secs = raw / 1e9 if raw > 1e11 else raw
    return (APPLE_EPOCH + timedelta(seconds=secs)).astimezone()


def _fmt_when(d: datetime) -> str:
    today = datetime.now().astimezone().date()
    if d.date() == today:
        return d.strftime("today %-I:%M %p")
    if d.date() == today - timedelta(days=1):
        return d.strftime("yesterday %-I:%M %p")
    return d.strftime("%a %b %-d %-I:%M %p")


def _decode_body(blob: bytes | None) -> str:
    """Newer macOS keeps the text only inside attributedBody (an NSArchiver 'typedstream')."""
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = blob.find(b"+", i) + 1
    if j <= 0 or j >= len(blob):
        return ""
    n = blob[j]
    j += 1
    if n == 0x81:
        n, j = int.from_bytes(blob[j:j + 2], "little"), j + 2
    elif n == 0x82:
        n, j = int.from_bytes(blob[j:j + 3], "little"), j + 3
    elif n == 0x83:
        n, j = int.from_bytes(blob[j:j + 4], "little"), j + 4
    return blob[j:j + n].decode("utf-8", "replace")


def _text(text: str | None, body: bytes | None) -> str:
    t = text or _decode_body(body)
    return t.replace("\ufffc", "[attachment]").strip()


def _key(handle: str) -> str:
    """Phone numbers match on their last 10 digits; emails on lowercase."""
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 7 else handle.strip().lower()


@cache
def _contacts() -> dict[str, str]:
    """Phone/email -> name, from the Contacts app's databases (empty if unreadable)."""
    names: dict[str, str] = {}
    for db in CONTACT_DBS.glob("**/AddressBook-v22.abcddb"):
        try:
            con = _connect(db)
            people = {pk: " ".join(p for p in (first, last) if p) or (org or "")
                      for pk, first, last, org in con.execute(
                          "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD")}
            rows = list(con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"))
            rows += list(con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"))
            con.close()
        except sqlite3.Error:
            continue
        for owner, value in rows:
            if value and people.get(owner):
                names.setdefault(_key(value), people[owner])
    return names


def _name(handle: str | None) -> str:
    if not handle:
        return "Unknown"
    return _contacts().get(_key(handle), handle)


MSG_FILTER = "m.associated_message_type = 0 AND m.item_type = 0"


def _chat_names(con: sqlite3.Connection) -> dict[int, str]:
    members: dict[int, list[str]] = {}
    for chat_id, handle in con.execute(
            "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"):
        members.setdefault(chat_id, []).append(_name(handle))
    out = {}
    for rowid, ident, display in con.execute("SELECT ROWID, chat_identifier, display_name FROM chat"):
        people = members.get(rowid) or [_name(ident)]
        out[rowid] = display or (", ".join(people[:4]) + (f" +{len(people) - 4}" if len(people) > 4 else ""))
    return out


def _run(fn, *args) -> str:
    try:
        con = _connect(CHAT_DB)
        con.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error as e:
        if "authorization denied" in str(e) or "unable to open" in str(e):
            return NO_ACCESS
        return f"Couldn't read Messages: {e}"
    try:
        return fn(con, *args)
    finally:
        con.close()


def _recent(con: sqlite3.Connection, limit: int) -> str:
    names = _chat_names(con)
    since = int((datetime.now(timezone.utc) - APPLE_EPOCH - timedelta(days=30)).total_seconds() * 1e9)
    rows = con.execute(f"""
        SELECT cmj.chat_id, MAX(m.date),
               SUM(CASE WHEN m.is_read = 0 AND m.is_from_me = 0 AND m.date > ? THEN 1 ELSE 0 END)
        FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
        WHERE {MSG_FILTER}
        GROUP BY cmj.chat_id ORDER BY MAX(m.date) DESC LIMIT ?""", (since, limit)).fetchall()
    lines = []
    for chat_id, last, unread in rows:
        text, body, mine, handle = con.execute(f"""
            SELECT m.text, m.attributedBody, m.is_from_me, h.id
            FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? AND {MSG_FILTER} ORDER BY m.date DESC LIMIT 1""", (chat_id,)).fetchone()
        who = "Me" if mine else _name(handle)
        snippet = " ".join(_text(text, body).split())[:120]
        badge = f" [{unread} unread]" if unread else ""
        lines.append(f"- {names.get(chat_id, '?')}{badge} ({_fmt_when(_when(last))}) {who}: {snippet}")
    return "\n".join(lines) or "No conversations found."


def _find_chats(con: sqlite3.Connection, who: str) -> list[int]:
    names = _chat_names(con)
    w = who.strip().lower()
    hits = [cid for cid, n in names.items() if w in n.lower()]
    if not hits:
        k = _key(who)
        hits = [cid for cid, ident in con.execute("SELECT ROWID, chat_identifier FROM chat")
                if _key(ident or "") == k]
    return hits


def _read(con: sqlite3.Connection, who: str, limit: int) -> str:
    who = people.handle(who, "phone", "email") or who
    chats = _find_chats(con, who)
    if not chats:
        return f"No conversation found for '{who}'. Try recent_texts to see the names."
    names = _chat_names(con)
    marks = ",".join("?" * len(chats))
    rows = con.execute(f"""
        SELECT cmj.chat_id, m.date, m.is_from_me, m.is_read, h.id, m.text, m.attributedBody
        FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id IN ({marks}) AND {MSG_FILTER}
        ORDER BY m.date DESC LIMIT ?""", (*chats, limit)).fetchall()
    shown = sorted({names.get(r[0], "?") for r in rows})
    out = [f"Conversation(s): {'; '.join(shown)}. Oldest first:"]
    for _chat, date, mine, read, handle, text, body in reversed(rows):
        who_ = "Me" if mine else _name(handle)
        new = " (unread)" if not mine and not read else ""
        out.append(f"[{_fmt_when(_when(date))}] {who_}{new}: {_text(text, body)}")
    return "\n".join(out)


def _search(con: sqlite3.Connection, query: str, limit: int) -> str:
    names = _chat_names(con)
    q = query.strip().lower()
    hits = []
    for chat_id, date, mine, handle, text, body in con.execute(f"""
            SELECT cmj.chat_id, m.date, m.is_from_me, h.id, m.text, m.attributedBody
            FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE {MSG_FILTER} ORDER BY m.date DESC LIMIT 50000"""):
        t = _text(text, body)
        if q in t.lower():
            who = "Me" if mine else _name(handle)
            hits.append(f"[{_fmt_when(_when(date))}] {names.get(chat_id, '?')} · {who}: {t[:300]}")
            if len(hits) >= limit:
                break
    return "\n".join(hits) or f"No texts contain '{query}'."


@function_tool
async def recent_texts(limit: int = 15) -> str:
    """List the most recent Messages conversations (texts and iMessage): who, when, unread count,
    and the last message. Read-only."""
    return await asyncio.to_thread(_run, _recent, max(1, min(limit, 50)))


@function_tool
async def read_texts(contact: str, limit: int = 30) -> str:
    """Read the latest messages in a Messages conversation. `contact` is a name (from Contacts),
    group name, phone number or email. Read-only."""
    return await asyncio.to_thread(_run, _read, contact, max(1, min(limit, 200)))


@function_tool
async def search_texts(query: str, limit: int = 20) -> str:
    """Search all Messages texts for words (newest first). Read-only."""
    return await asyncio.to_thread(_run, _search, query, max(1, min(limit, 50)))


SEND_SCRIPT = """
on run argv
  tell application "Messages"
    if (item 3 of argv) is "SMS" then
      set svc to 1st account whose service type = SMS
    else
      set svc to 1st account whose service type = iMessage
    end if
    send (item 1 of argv) to participant (item 2 of argv) of svc
  end tell
end run
"""


ME = re.compile(r"^(?:me|myself|self|(?:my|your)(?: own)?(?: number| phone(?: number)?)?|(?:my|your) own)$", re.I)


def _me(con: sqlite3.Connection) -> tuple[str, str] | str:
    """the user’s own number/Apple ID: the account most of his texts go out from."""
    row = con.execute("""SELECT account FROM message WHERE is_from_me = 1 AND account LIKE '_:%'
                         GROUP BY account ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
    if not row:
        return "Couldn't find your own number in Messages. Tell me your number once."
    return row[0].split(":", 1)[1], "iMessage"


def _recipient(con: sqlite3.Connection, who: str) -> tuple[str, str] | str:
    """One person's (number/email, service), or an explanation when it's unclear."""
    if ME.match(who.strip()):
        return _me(con)
    who = people.handle(who, "phone", "email") or who
    w, k = who.strip().lower(), _key(who)
    found = {}
    for ident, svc, last in con.execute("""
            SELECT c.chat_identifier, c.service_name, MAX(j.message_date) FROM chat c
            JOIN chat_message_join j ON j.chat_id = c.ROWID WHERE c.style = 45
            GROUP BY c.ROWID ORDER BY MAX(j.message_date) DESC"""):
        if ident and (_name(ident).lower() == w or _key(ident) == k):
            found.setdefault(_key(ident), (ident, svc or "iMessage"))
    if not found:
        names = {n for h, n in _contacts().items() if w in n.lower()}
        hint = f" Did you mean: {', '.join(sorted(names)[:5])}?" if names else ""
        return f"No existing conversation with exactly '{who}'.{hint} Use their full name or number."
    if len(found) > 1:
        return f"'{who}' has several numbers/emails: {', '.join(i for i, _ in found.values())}. Ask which one."
    return next(iter(found.values()))


def _send(con: sqlite3.Connection, who: str, message: str) -> str:
    target = _recipient(con, who)
    if isinstance(target, str):
        return target
    handle, svc = target
    if subprocess.run(["pgrep", "-x", "Messages"], capture_output=True).returncode:
        subprocess.run(["open", "-g", "-a", "Messages"])
        time.sleep(3)
    start = int((time.time() - 978307200 - 5) * 1e9)
    r = subprocess.run(["osascript", "-", message, handle, "SMS" if svc == "SMS" else "iMessage"],
                       input=SEND_SCRIPT, capture_output=True, text=True, timeout=120)
    if r.returncode:
        err = r.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            return ("macOS blocked it: allow Mac Copilot (or Terminal) to control Messages in System Settings → "
                    "Privacy & Security → Automation, then try again.")
        return f"Messages couldn't send it: {err[:200]}"
    for _ in range(20):
        row = con.execute("""
            SELECT m.error, m.is_sent, m.is_delivered FROM message m
            JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID = j.chat_id
            WHERE c.chat_identifier = ? AND m.is_from_me = 1 AND m.date > ?
            ORDER BY m.date DESC LIMIT 1""", (handle, start)).fetchone()
        if row and row[0]:
            return f"Messages reported an error sending to {_name(handle)} ({handle})."
        if row and (row[1] or row[2]):
            return f"Sent to {_name(handle)} ({handle}): {message}" + (" — delivered." if row[2] else ".")
        time.sleep(1)
    return f"Handed to Messages for {_name(handle)} ({handle}), but it hasn't confirmed sending yet."


@function_tool
async def send_text(contact: str, message: str) -> str:
    """Send a text / iMessage to one person you already have a conversation with (name or number).
    contact "me" texts the user's own number. Only call this after the user has confirmed the exact recipient and wording."""
    if why := decisions.gate("text", contact, message):
        return why
    r = await asyncio.to_thread(_run, _send, contact, message)
    decisions.sent("text", contact, message, r)
    return r


def text_me(message: str) -> str:
    """An alert the user asked for (a page watch firing), texted to his own number. Never to anyone else."""
    return _run(_send, "me", message)


MESSAGE_TOOLS = [recent_texts, read_texts, search_texts, send_text]

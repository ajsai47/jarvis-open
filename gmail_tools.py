"""Gmail: read, search and send through the Gmail API (no Mail window needed).

Run `Connect Gmail.command` (gmail_auth.py) once to create gmail_token.json.
"""

import asyncio
import base64
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path

from agents import function_tool

import decisions
import people

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "gmail_token.json"
CLIENT_FILE = HERE / "google_client.json"
API = "https://gmail.googleapis.com/gmail/v1/users/me/"
NOT_CONNECTED = "Gmail isn't connected yet: double-click 'Connect Gmail.command' and click Allow."
_access = {"token": "", "until": 0.0, "file": 0.0}


class GmailError(Exception):
    pass


def _token() -> str:
    try:
        signed_in = TOKEN_FILE.stat().st_mtime  # a new sign-in (e.g. adding Calendar) needs a new access token
    except OSError:
        raise GmailError(NOT_CONNECTED)
    if _access["token"] and time.time() < _access["until"] and _access["file"] == signed_in:
        return _access["token"]
    try:
        refresh = json.loads(TOKEN_FILE.read_text())["refresh_token"]
        client = json.loads(CLIENT_FILE.read_text())["installed"]
    except (OSError, ValueError, KeyError):
        raise GmailError(NOT_CONNECTED)
    body = urllib.parse.urlencode({"client_id": client["client_id"], "client_secret": client["client_secret"],
                                   "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(client["token_uri"], data=body, timeout=20) as r:
            tok = json.load(r)
    except urllib.error.HTTPError:
        raise GmailError("Gmail sign-in expired: double-click 'Connect Gmail.command' again.")
    _access.update(token=tok["access_token"], until=time.time() + tok.get("expires_in", 3600) - 120, file=signed_in)
    return _access["token"]


def _api(path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code == 403 and body is not None:
            raise GmailError("Gmail won't let Mac Copilot send yet: double-click 'Connect Gmail.command' and allow sending.")
        raise GmailError(f"Gmail API {e.code}: {detail}")


def _safe(fn, *args) -> str:
    try:
        return fn(*args)
    except GmailError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"Gmail failed: {str(e)[:200]}"


def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _summary(msg: dict) -> str:
    h = _headers(msg)
    unread = " [unread]" if "UNREAD" in msg.get("labelIds", []) else ""
    return (f"- id={msg['id']}{unread} {h.get('date', '')[:25]} | From: {h.get('from', '?')} | "
            f"Subject: {h.get('subject', '(no subject)')} | {html.unescape(msg.get('snippet', ''))[:160]}")


def _list(query: str, limit: int) -> str:
    ids = _api("messages", {"q": query, "maxResults": limit}).get("messages", [])
    if not ids:
        return f"No emails match {query!r}."
    _token()  # refresh once up front, not in every thread
    with ThreadPoolExecutor(8) as pool:  # one request per email, so fetch them side by side
        rows = list(pool.map(lambda m: _summary(_api("messages/" + m["id"], {
            "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})), ids))
    return f"{len(rows)} email(s) for {query!r}, newest first:\n" + "\n".join(rows)


def _text(part: dict) -> str:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        raw = base64.urlsafe_b64decode(data + "===").decode(errors="replace")
        if mime == "text/html":
            raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        return raw
    kids = part.get("parts", [])
    plain = next((k for k in kids if k.get("mimeType") == "text/plain"), None)
    for k in ([plain] if plain else []) + kids:
        t = _text(k)
        if t.strip():
            return t
    return ""


def _read(message_id: str) -> str:
    msg = _api("messages/" + message_id, {"format": "full"})
    h = _headers(msg)
    body = re.sub(r"\n\s*\n+", "\n\n", _text(msg.get("payload", {}))).strip()
    return (f"From: {h.get('from')}\nTo: {h.get('to')}\nDate: {h.get('date')}\nSubject: {h.get('subject')}\n"
            f"Thread: {msg.get('threadId')}\n\n{body[:4000]}")


def _addrs(s: str) -> str:
    """Names or nicknames from people.json become their email address."""
    out = []
    for part in (x.strip() for x in s.split(",")):
        if part and "@" not in part:
            addr = people.handle(part, "email")
            if not addr:
                raise GmailError(f"No email address on file for {part!r}. Ask the user for it (and offer to remember it).")
            part = f"{people.find(part)[0]['name']} <{addr}>"
        if part:
            out.append(part)
    return ", ".join(out)


def _send(to: str, subject: str, body: str, reply_to_id: str, cc: str) -> str:
    to, cc = _addrs(to), _addrs(cc)
    m = EmailMessage()
    payload: dict = {}
    if reply_to_id:
        orig = _api("messages/" + reply_to_id, {"format": "metadata", "metadataHeaders":
                                                ["From", "Reply-To", "Subject", "Message-ID", "References"]})
        h = _headers(orig)
        to = to or h.get("reply-to") or h.get("from", "")
        subj = h.get("subject", "")
        subject = subject or (subj if subj.lower().startswith("re:") else "Re: " + subj)
        if h.get("message-id"):
            m["In-Reply-To"] = h["message-id"]
            m["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
        payload["threadId"] = orig["threadId"]
    if not to:
        raise GmailError("Who should this go to? No recipient given.")
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject or "(no subject)"
    m.set_content(body)
    payload["raw"] = base64.urlsafe_b64encode(m.as_bytes()).decode()
    sent = _api("messages/send", body=payload)
    return f"Sent email to {to}{' (reply in thread)' if reply_to_id else ''}: {m['Subject']!r} (id {sent.get('id')})."


@function_tool
async def email_inbox(days: int = 2, unread_only: bool = True, limit: int = 15) -> str:
    """List recent emails in the Gmail inbox (from, subject, snippet, id), newest first."""
    q = f"in:inbox newer_than:{max(1, min(days, 30))}d" + (" is:unread" if unread_only else "")
    return await asyncio.to_thread(_safe, _list, q, max(1, min(limit, 30)))


@function_tool
async def search_email(query: str, limit: int = 10) -> str:
    """Search Gmail with Gmail search syntax, e.g. 'from:julius', 'subject:invoice newer_than:7d'."""
    return await asyncio.to_thread(_safe, _list, query, max(1, min(limit, 30)))


@function_tool
async def read_email(message_id: str) -> str:
    """Read one email's full text, by the id from email_inbox or search_email."""
    return await asyncio.to_thread(_safe, _read, message_id)


@function_tool
async def send_email(to: str, subject: str, body: str, reply_to_id: str = "", cc: str = "") -> str:
    """Send an email from the user's Gmail. For a reply pass reply_to_id (the email's id); `to` and
    `subject` can then be empty and default to the original sender and 'Re: …'.
    Only call this after the user has confirmed the exact recipient, subject and wording."""
    if why := decisions.gate("email", to or "(reply to the sender)", body, subject):
        return why
    r = await asyncio.to_thread(_safe, _send, to, subject, body, reply_to_id, cc)
    decisions.sent("email", to or "(reply)", body, r, subject=subject, cc=cc)
    return r


EMAIL_TOOLS = [email_inbox, search_email, read_email, send_email]

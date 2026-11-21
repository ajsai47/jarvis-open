"""the user’s people (people.json, git-ignored, 0600): who "Mom", "Jess" or "my cofounder" is, so Jarvis knows
who the user means. Names, nicknames and who they are go into every request's instructions; phone numbers,
emails and Slack ids only come out when a tool needs them. Changes are logged to the decision log
without contact details. Edited in Settings → People or by asking Jarvis."""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses
from pathlib import Path

import decisions

FILE = Path(__file__).resolve().parent / "people.json"
FIELDS = ("name", "aka", "who", "group", "phone", "email", "slack", "notes")
GROUPS = ("Work", "Life")
_lock = threading.Lock()


def _clean(p: dict) -> dict | None:
    out: dict = {}
    for k in FIELDS:
        v = p.get(k)
        if k == "aka":
            v = v if isinstance(v, list) else str(v or "").split(",")
            v = [" ".join(str(x).split())[:60] for x in v if str(x).strip()][:12]
        elif k == "group":
            v = next((g for g in GROUPS if g.lower() == str(v or "").strip().lower()), "")
        else:
            v = " ".join(str(v or "").split())[:400 if k == "notes" else 120]
        if v:
            out[k] = v
    return out if out.get("name") else None


def load() -> list[dict]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []
    return [c for p in data if isinstance(p, dict) and (c := _clean(p))] if isinstance(data, list) else []


def _public(p: dict) -> str:
    """Name, nicknames and who they are: what may go into instructions and the decision log."""
    return p["name"] + (f" ({', '.join(p['aka'])})" if p.get("aka") else "") + (f": {p['who']}" if p.get("who") else "")


def save(people: list[dict], how: str = "settings") -> list[dict]:
    clean, seen = [], set()
    for p in people:
        if isinstance(p, dict) and (c := _clean(p)) and c["name"].lower() not in seen:
            seen.add(c["name"].lower())
            clean.append(c)
    with _lock:
        old = load()
        if clean == old:
            return old
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(FILE)
    before, after = {_public(p) for p in old}, {_public(p) for p in clean}
    for s in sorted(before - after):
        decisions.record("person", verdict="removed", final=s, request=how)
    for s in sorted(after - before):
        decisions.record("person", verdict="added", final=s, request=how)
    return clean


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("'s ", " ")).split())


def find(ref: str) -> list[dict]:
    """People the user could mean by `ref`: a name, nickname ("Mom") or description ("my cofounder")."""
    r = re.sub(r"^(my|the|our) ", "", _norm(ref))
    if not r:
        return []
    people = load()

    def names(p):
        return [_norm(p["name"]), *(_norm(a) for a in p.get("aka", []))]

    for test in (lambda p: r in names(p),
                 lambda p: any(n.split()[:1] == [r] for n in names(p)),
                 lambda p: p.get("who") and re.search(rf"\b{re.escape(r)}\b", _norm(p["who"]))):
        hits = [p for p in people if test(p)]
        if hits:
            return hits
    return []


def handle(ref: str, *fields: str) -> str | None:
    """The first of `fields` on file for the one person `ref` means; None if nobody or several match."""
    hits = find(ref)
    if len(hits) != 1:
        return None
    return next((hits[0][f] for f in fields if hits[0].get(f)), None)


def block() -> str:
    ps = load()
    if not ps:
        return ""
    lines = []
    for g in (*GROUPS, ""):
        group = [p for p in ps if p.get("group", "") == g]
        if group:
            lines.append(f"{g or 'Not sorted'} (most important first):")
        for p in group[:300]:
            has = [k for k in ("phone", "email", "slack") if p.get(k)]
            lines.append("- " + _public(p) + (f" [{', '.join(has)} on file]" if has else ""))
    return ("\n\nAJ's people (when he names or describes someone, look here first). Pass their name or nickname "
            "to send_text, read_texts, send_email or slack_send and it resolves to the number, email or Slack "
            "account on file; list_people shows the details. If a reference fits more than one person, ask which, "
            "naming the more important one first. Work is the user’s work (work); Life is family and friends:\n"
            + "\n".join(lines) + "\n")


# ---- starter list: who the user texts, emails and Slacks most (counts only; no message text is read)

def draft(limit: int = 30) -> dict:
    cands, errors = _collect(skip_known=True)
    picked: dict[int, dict] = {}
    for key, n in (("texts", limit // 2), ("emails", limit // 3), ("slack_msgs", limit // 3)):  # every source gets a share
        for c in sorted((c for c in cands.values() if c[key]), key=lambda c: -c[key])[:n]:
            picked[id(c)] = c
    out = sorted(picked.values(), key=lambda c: -(c["texts"] + 2 * c["emails"] + c["slack_msgs"]))
    return {"people": out[:limit], "errors": errors}


def fill() -> dict:
    """Phone numbers, emails and Slack accounts that saved people are missing, matched by name."""
    import messages_tools as mt

    cands, errors = _collect(skip_known=False)
    book: dict[str, dict] = {}  # Mac Contacts, by name
    try:
        for value, name in mt._contacts().items():
            k = "email" if "@" in value else "phone"
            v = ("+1" + value if len(value) == 10 and value.isdigit() else value) if k == "phone" else value
            book.setdefault(_norm(name), {}).setdefault(k, v)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Contacts: {e}")
    out = []
    for p in load():
        n = _norm(p["name"])
        found = []
        for src in (book, cands):
            c = src.get(n)
            if not c and len(n.split()) == 1:
                first = [v for k, v in src.items() if k.split()[:1] == [n]]
                c = first[0] if len(first) == 1 else None
            found.append(c or {})
        c = {**found[0], **{k: v for k, v in found[1].items() if v}}
        new = {k: c[k] for k in ("phone", "email", "slack") if c.get(k) and not p.get(k)}
        if new:
            out.append({"name": p["name"], **new})
    return {"fills": out, "errors": errors}


def _collect(skip_known: bool) -> tuple[dict, list]:
    import gmail_tools as gt
    import messages_tools as mt
    import slack_tools as st

    have = load()
    known = {_norm(p["name"]) for p in have} | {str(p.get(k, "")).lower() for p in have for k in ("email", "slack")}
    known |= {mt._key(p["phone"]) for p in have if p.get("phone")}
    cands: dict[str, dict] = {}
    errors = []

    def add(name: str, field: str, value: str, count: str, n: int):
        if not value or (skip_known and (value.lower() in known or (field == "phone" and mt._key(value) in known))):
            return
        c = cands.setdefault(_norm(name) or value.lower(), {"name": name, "texts": 0, "emails": 0, "slack_msgs": 0})
        c.setdefault(field, value)
        c[count] += n

    try:
        con = mt._connect(mt.CHAT_DB)
        since = int((time.time() - 978307200 - 180 * 86400) * 1e9)
        rows = con.execute("""
            SELECT c.chat_identifier, COUNT(*) FROM chat c JOIN chat_message_join j ON j.chat_id = c.ROWID
            JOIN message m ON m.ROWID = j.message_id WHERE c.style = 45 AND m.date > ? AND m.item_type = 0
            GROUP BY c.chat_identifier ORDER BY 2 DESC LIMIT 60""", (since,)).fetchall()
        con.close()
        for ident, n in rows:
            add(mt._name(ident), "email" if "@" in ident else "phone", ident, "texts", n)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Texts: {str(e)[:120]}")
    try:
        me = gt._api("profile").get("emailAddress", "").lower()
        ids = gt._api("messages", {"q": "in:sent newer_than:180d", "maxResults": 300}).get("messages", [])
        with ThreadPoolExecutor(8) as pool:
            heads = list(pool.map(lambda m: gt._headers(gt._api("messages/" + m["id"], {
                "format": "metadata", "metadataHeaders": ["To", "Cc"]})), ids))
        counts: dict[str, list] = {}
        for h in heads:
            for nm, addr in getaddresses([h.get("to", ""), h.get("cc", "")]):
                a = addr.lower()
                if a and a != me and not re.search(r"no-?reply|notifications?@|calendar", a):
                    counts.setdefault(a, [nm, 0])[1] += 1
        for a, (nm, n) in sorted(counts.items(), key=lambda kv: -kv[1][1])[:60]:
            add(nm or mt._contacts().get(a) or a.split("@")[0], "email", a, "emails", n)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Email: {str(e)[:120]}")
    try:
        ims: dict[str, int] = {}
        for page in (1, 2, 3):
            d = st._api("search.messages", query="from:me", count=100, page=page, sort="timestamp")
            for m in d.get("messages", {}).get("matches", []):
                ch = m.get("channel", {})
                uid = ch.get("user") or ch.get("name", "")
                if ch.get("is_im") and re.fullmatch(r"[UW][A-Z0-9]{6,}", uid):
                    ims[uid] = ims.get(uid, 0) + 1
            if page >= d.get("messages", {}).get("paging", {}).get("pages", 1):
                break
        me_id = st._me()[0]
        for uid, n in sorted(ims.items(), key=lambda kv: -kv[1])[:40]:
            if uid != me_id:
                add(st._user_name(uid), "slack", uid, "slack_msgs", n)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Slack: {str(e)[:120]}")
    return cands, errors

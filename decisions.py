"""Jarvis's decision log: what the user approved, said no to or changed, with the context it happened in.
Kept on this Mac in logs/decisions.jsonl (git-ignored, 0600) and copied to Claude Mem (local) so
later requests can learn from it. Anything involving texts stays in the file only. export() turns
the log into training rows (request, Jarvis's draft, the user’s answer, what was finally sent)."""

import contextvars
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

FILE = Path(__file__).resolve().parent / "logs" / "decisions.jsonl"
CLAUDE_MEM = "http://127.0.0.1:37701/api/memory/save"
PRIVATE = {"text", "voice_private"}  # never leaves the file, and left out of exports unless asked for
# The answer that led to the current action (set per task): {"request", "draft", "answer"}
CTX: contextvars.ContextVar = contextvars.ContextVar("decision", default=None)
# Where show_draft puts the exact message of the current task's draft: {"channel", "to", "subject", "message"}
BOX: contextvars.ContextVar = contextvars.ContextVar("draft_box", default=None)
_YES = re.compile(r"^\s*(?:(?:yes|yep|yeah|sure|ok(?:ay)?|go ahead|send it|do it|start it|approved?|confirm(?:ed)?|please)\b[\s,.!]*)+$", re.I)
_NO = re.compile(r"^\s*(no|nope|cancel|don'?t|stop)\b", re.I)
_lock = threading.Lock()
# Voice has no task card, so its drafts and answers are paired here from what was heard and said.
_CONFIRM = re.compile(r"\b(send|post|delete|submit|buy|publish|reply|go ahead|start|launch)\b[^?\n]{0,40}\?\s*$", re.I)
_VOICE: dict = {"heard": [], "draft": None}
FRESH_S = 600


def verdict(answer: str) -> str:
    """yes = approved as drafted, no = rejected, changed = approved with changes or other instructions."""
    return "yes" if _YES.search(answer) else "no" if _NO.search(answer) else "changed"


def record(kind: str, channel: str = "", **fields) -> dict:
    rec = {"at": round(time.time(), 3), "kind": kind, "channel": channel,
           **{k: v for k, v in fields.items() if v not in (None, "")}}
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        new = not FILE.exists()
        with open(FILE, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        if new:
            os.chmod(FILE, 0o600)
    if channel not in PRIVATE and not os.environ.get("JARVIS_NO_CLAUDE_MEM"):
        threading.Thread(target=_to_claude_mem, args=(rec,), daemon=True).start()
    return rec


# the user said up front not to ask: "just send it", "don't ask", "no need to confirm".
_JUST = re.compile(r"\b(?:just send(?: it)?|send it (?:now|right away|straight away)|don'?t (?:bother )?(?:ask|confirm)(?:ing)?"
                   r"|no need to (?:ask|confirm|check)|without (?:asking|confirming|checking))\b", re.I)


def gate(channel: str = "", to: str = "", message: str = "", subject: str = "") -> str:
    """Why a send must not happen yet ("" = go ahead), for every model. A send goes ahead only after the user’s yes
    to this draft (typed, on the card, or spoken), his own edited text, or a request that said "just send it".
    A blocked send becomes the draft card, so the user sees (and can edit) exactly what the model tried to send."""
    ctx = CTX.get()
    if ctx is not None:
        if ctx.get("answer") and verdict(ctx["answer"]) == "yes":
            return ""
        if ctx.get("edited") and message.strip() == ctx["edited"].strip():  # the user approved his own exact text
            return ""
        if _JUST.search(ctx.get("request") or ""):
            return ""
    else:  # voice: the answer to the draft Jarvis read back, or what the user just said
        d, now = _VOICE["draft"], time.time()
        if d and d.get("answer") and now - d["at"] < FRESH_S and verdict(d["answer"]) != "no":
            return ""
        if any(_JUST.search(x) for t, x in _VOICE["heard"] if now - t < 300):
            return ""
    if message and to.strip():
        msg = {"channel": channel, "to": to, "subject": subject, "message": message}
        propose(**msg)
        import helpers
        try:
            helpers.EMIT.get()(type="draft", **msg)
        except LookupError:
            pass
        return ("Not sent yet: Jarvis is holding this exact draft for the user. In one short line say who it's to "
                "and the wording, end with \"Send it?\" and wait for the answer.")
    return "Not sent. Jarvis sends only after a yes to the exact draft: show the draft and ask \"Send it?\"."


def heard(text: str) -> None:
    """Something the user said by voice: the answer to a waiting spoken draft, or part of a new request."""
    now, v = time.time(), _VOICE
    d = v["draft"]
    if d and not d.get("answer") and now - d["at"] < FRESH_S:
        d["answer"] = text
        channel = (d.get("msg") or {}).get("channel") or "voice_private"
        record("answer", channel, verdict=verdict(text), via="voice",
               **{k: d.get(k) for k in ("request", "draft", "msg", "answer")})
        v["heard"] = []
        return
    v["heard"] = [(t, x) for t, x in v["heard"] if now - t < 300][-3:] + [(now, text)]


def propose(**msg) -> None:
    """The exact message Jarvis is about to ask about (from show_draft)."""
    box = BOX.get()
    if box is not None:
        box.clear()
        box.update(msg)
    else:
        _VOICE["msg"] = msg


def said(text: str) -> None:
    """Something Jarvis said by voice. A reply asking "Send it?" is a draft waiting for the user’s answer."""
    if _CONFIRM.search(text.strip()):
        _VOICE["draft"] = {"request": " / ".join(x for _, x in _VOICE["heard"]), "draft": text,
                           "msg": _VOICE.pop("msg", None), "at": time.time()}


def _context() -> dict:
    ctx = CTX.get()
    if ctx is not None:
        return ctx
    d = _VOICE["draft"]
    return d if d and time.time() - d["at"] < FRESH_S else {}


def sent(channel: str, to: str, message: str, result: str, **extra) -> None:
    """Called by every tool that sends or starts something, after it ran."""
    ctx = _context()
    record("sent", channel, to=to, final=message, draft=ctx.get("draft"), request=ctx.get("request"),
           answer=ctx.get("answer"), msg=ctx.get("msg"), edited=ctx.get("edited"), run=ctx.get("run"),
           result=str(result)[:300], **extra)
    if CTX.get() is None:  # a spoken yes or "just send it" covers one send
        _VOICE.update(draft=None, heard=[])


def _to_claude_mem(rec: dict) -> None:
    what = {"answer": f"the user answered \u201c{rec.get('answer', '')}\u201d ({rec.get('verdict')})",
            "sent": f"Jarvis sent to {rec.get('to', '?')} on {rec['channel']} after the user’s OK",
            "checkin": f"the user answered a check-in: {rec.get('verdict')}"}.get(rec["kind"], rec["kind"])
    body = "\n".join(f"{k}: {str(v)[:1500]}" for k, v in rec.items() if k not in ("at",))
    data = {"text": f"{what}.\n{body}", "title": f"Jarvis decision: {what}"[:120], "project": "jarvis-decisions",
            "metadata": {"source": "jarvis", "kind": rec["kind"], "channel": rec["channel"]}}
    try:
        urllib.request.urlopen(urllib.request.Request(CLAUDE_MEM, data=json.dumps(data).encode(),
                               headers={"Content-Type": "application/json"}), timeout=5).close()
    except Exception:
        pass


def load() -> list:
    if not FILE.exists():
        return []
    out = []
    for line in FILE.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def export(texts: bool = False) -> list:
    """One row per decision: the request, Jarvis's draft, the user’s answer and the final action.
    A row where draft and final differ is a preference pair ("this, not that")."""
    rows = []
    for r in load():
        if r.get("channel") == "voice_private" or (r.get("channel") in PRIVATE and not texts):
            continue
        rows.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r["at"])), "kind": r["kind"],
                     "channel": r.get("channel", ""), "request": r.get("request", ""), "draft": r.get("draft", ""),
                     "answer": r.get("answer", ""), "verdict": r.get("verdict", "yes" if r["kind"] == "sent" else ""),
                     "final": r.get("final", ""), "to": r.get("to", ""),
                     "draft_message": (r.get("msg") or {}).get("message", ""), "edited": r.get("edited", ""),
                     "run": r.get("run", ""), "pair": _pair(r)})
    return rows


def _pair(r: dict) -> bool:
    """True when what was sent differs from what Jarvis first drafted: a "this, not that" example."""
    first, final = ((r.get("msg") or {}).get("message") or "").strip(), (r.get("final") or "").strip()
    if first and final:
        return first != final
    return bool(r.get("draft") and final and verdict(r.get("answer") or "yes") == "changed")


def count() -> dict:
    rs = load()
    return {"total": len(rs), "texts": sum(r.get("channel") in PRIVATE for r in rs)}

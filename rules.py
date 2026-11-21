"""the user’s standing rules for Jarvis: one per line in rules.md (git-ignored), added to every request's
instructions, so a change applies from the next request without a restart. Edited in Settings → Rules
or by asking Jarvis; every change also goes into the decision log."""

import threading
from pathlib import Path

import decisions
import people

FILE = Path(__file__).resolve().parent / "rules.md"
HEAD = "# Jarvis rules\nOne rule per line. Jarvis follows these on every request.\n\n"
MAX = 500  # characters per rule
_lock = threading.Lock()


def load() -> list[str]:
    try:
        lines = FILE.read_text().splitlines()
    except OSError:
        return []
    return [s[2:].strip() for s in lines if s.startswith("- ") and s[2:].strip()]


def save(rules: list[str], how: str = "settings") -> list[str]:
    clean = []
    for r in rules:
        r = " ".join(str(r).split())[:MAX]
        if r and r not in clean:
            clean.append(r)
    with _lock:
        old = load()
        if clean == old:
            return old
        FILE.write_text(HEAD + "".join(f"- {r}\n" for r in clean))
    for r in old:
        if r not in clean:
            decisions.record("rule", verdict="removed", final=r, request=how)
    for r in clean:
        if r not in old:
            decisions.record("rule", verdict="added", final=r, request=how)
    return clean


def block() -> str:
    rules = load()
    out = "" if not rules else ("\n\nAJ's rules (always follow these; they override the defaults above):\n"
                                + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) + "\n")
    return out + people.block()


def instructions(base: str):
    """Instructions for an Agent: base plus the current rules, read fresh for every request."""
    return lambda ctx, agent: base + block()

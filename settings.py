"""Jarvis settings, kept on this Mac in logs/settings.json (git-ignored). Missing keys use DEFAULTS."""

import json
import os
import time
from pathlib import Path

FILE = Path(__file__).resolve().parent / "logs" / "settings.json"
DEFAULTS = {
    "helpers": 12,         # helpers running at once; extra jobs wait for a free slot
    "plan_steps": 30,      # most steps one message can turn into
    "memory_turns": 200,   # back-and-forths Jarvis remembers across requests
    "memory_chars": 200_000,  # ...but never more text than this (keeps each request fast)
    "model_quick": "gpt-5.5",  # looking things up (read-only requests)
    "model_work": "gpt-5.5",   # everything else: tasks, drafts, helpers, what voice hands off
    "model_voice": "gpt-realtime-2.1",
    "voice_engine": "openai",  # "openai" (live voice) or "local" (Whisper, a voice brain and Kokoro on this Mac)
    "model_brain": "local:gpt-oss:20b",  # what does spoken requests when the voice engine is "local"
    "voice_speed": 1.2,  # how fast George talks on the On this Mac engine
    "voice_muted": False,  # Jarvis answers in text only, without speaking
    "hush_media": True,  # mute playing Dia tabs and pause Spotify / Music while Jarvis talks
}
LOCAL = "local:"  # a model running on this Mac through Ollama
OLLAMA = "http://127.0.0.1:11434"
MODELS = {
    "quick": ["gpt-5.5", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5.4-nano",
              "gpt-4.1-mini", LOCAL + "gpt-oss:20b"],
    "work": ["gpt-5.5", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5-pro", "gpt-5.4",
             LOCAL + "gpt-oss:20b"],
    "voice": ["gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2"],
    "brain": [LOCAL + "gpt-oss:20b", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-luna"],
}
ENGINES = ("openai", "local")
SPEEDS = (1.0, 1.1, 1.2, 1.3, 1.5)
LIMITS = {"helpers": (1, 32), "plan_steps": (2, 100), "memory_turns": (10, 2000), "memory_chars": (20_000, 1_000_000)}
_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is None:
        try:
            saved = json.loads(FILE.read_text())
        except (OSError, ValueError):
            saved = {}
        _cache = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    return _cache


def get(key: str):
    return load()[key]


def update(changes: dict) -> dict:
    s = dict(load())
    for k, v in changes.items():
        if k.startswith("model_") and k in DEFAULTS:
            if v not in MODELS[k[6:]]:
                raise ValueError("Pick one of the listed models.")
            s[k] = v
        elif k == "voice_engine":
            if v not in ENGINES:
                raise ValueError("Pick one of the listed voice engines.")
            s[k] = v
        elif k == "voice_speed":
            if float(v) not in SPEEDS:
                raise ValueError("Pick one of the listed speeds.")
            s[k] = float(v)
        elif k in ("voice_muted", "hush_media"):
            s[k] = bool(v)
        elif k in LIMITS:
            lo, hi = LIMITS[k]
            s[k] = max(lo, min(hi, int(v)))
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(FILE)
    global _cache
    _cache = s
    return s


_clients: dict = {}


FALLBACK = "gpt-5.5"  # used instead of a local model while Ollama isn't running
_ollama = {"at": 0.0, "up": False}


def ollama_up() -> bool:
    """Is Ollama answering? Checked at most every 10 s."""
    import urllib.request
    if time.time() - _ollama["at"] > 10:
        try:
            with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=0.5):
                _ollama["up"] = True
        except OSError:
            _ollama["up"] = False
        _ollama["at"] = time.time()
    return _ollama["up"]


def ollama_down() -> None:
    _ollama.update(at=time.time(), up=False)


def model(kind: str) -> str:
    """The chosen model for this kind, or the cloud fallback if it's local and Ollama is down."""
    m = get("model_" + kind)
    return FALLBACK if m.startswith(LOCAL) and not ollama_up() else m


# $ per 1M tokens (input, cached input, output), from platform.openai.com/docs/models, Nov 2026
PRICES = {"gpt-5.5": (5.00, 0.50, 30.00), "gpt-5.4-mini": (0.75, 0.075, 4.50)}


def usage(model: str, u) -> dict:
    """Tokens one request used and, for priced cloud models, what it cost."""
    cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
    out = {"model": model, "in": u.input_tokens, "cached": cached, "out": u.output_tokens,
           "reasoning": getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", 0) or 0}
    if model in PRICES:
        p = PRICES[model]
        out["usd"] = round(((u.input_tokens - cached) * p[0] + cached * p[1] + u.output_tokens * p[2]) / 1e6, 5)
    elif model.startswith(LOCAL):
        out["usd"] = 0.0
    return out


def agent_model(kind: str):
    """The model for "quick" or "work", ready for an Agent. Local ones talk to Ollama on this Mac."""
    m = model(kind)
    if not m.startswith(LOCAL):
        return m
    if m not in _clients:
        from agents import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI
        _clients[m] = OpenAIChatCompletionsModel(
            model=m[len(LOCAL):], openai_client=AsyncOpenAI(base_url=OLLAMA + "/v1", api_key="ollama"))
    return _clients[m]


def keep_warm() -> None:
    """Keeps the local models in use loaded in Ollama. Each request through its OpenAI-style API resets
    the unload timer to 5 min, so a cold model (~1.5 s to load, ~4 s to re-read the instructions) is
    what made the first request after a break slow."""
    import urllib.request
    use = {get("model_quick"), get("model_work")}
    if get("voice_engine") == "local":
        use.add(get("model_brain"))
    for m in sorted(x for x in use if x.startswith(LOCAL)):
        if not ollama_up():
            return
        try:
            urllib.request.urlopen(urllib.request.Request(
                OLLAMA + "/api/generate", json.dumps({"model": m[len(LOCAL):], "keep_alive": "30m"}).encode(),
                {"Content-Type": "application/json"}), timeout=120).read()
        except (OSError, ValueError):
            pass


def is_local(kind: str) -> bool:
    return model(kind).startswith(LOCAL)


def label(kind: str) -> str:
    m = model(kind)
    return m[len(LOCAL):] + " (this Mac)" if m.startswith(LOCAL) else m


def local_models() -> list:
    """Models Ollama has on this Mac ([] if Ollama isn't running)."""
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=1) as r:
            return [x["name"] for x in json.load(r).get("models", [])]
    except (OSError, ValueError):
        return []

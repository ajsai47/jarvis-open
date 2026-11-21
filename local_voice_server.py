"""Jarvis's on-device ears (Whisper) and voice (Kokoro), kept warm in their own process and venv
(.venv-voice, requirements-voice.txt) so their heavy libraries never touch Jarvis's.
Listens on 127.0.0.1 only. POST /stt (audio bytes) -> {"text"}; POST /tts {"text"} -> 24 kHz audio
(raw 16-bit PCM, or WAV with ?wav=1). JARVIS_VOICE_FAKE=1 (or no MLX) gives canned answers for tests."""

import io
import json
import os
import re
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

PORT = int(os.environ.get("JARVIS_VOICE_PORT", "8767"))
WHISPER = "mlx-community/whisper-large-v3-turbo"
KOKORO = "mlx-community/Kokoro-82M-bf16"
VOICE = os.environ.get("JARVIS_VOICE", "bm_george")
RATE = 24000
# names Whisper should expect; the fixes catch what it still mishears
HINT = ("the user talking to Jarvis, his Mac assistant. Names: Dia (a browser), Slack, Supabase, Devin, Codex, Claude, "
        "Vercel, Mission Control, Work, tRPC, Notion, Linear.")
FIXES = [(r"\bsuper ?base\b", "Supabase"), (r"\bdevon\b", "Devin"), (r"\bjarvis's\b", "Jarvis's"),
         (r"\bin (?:india|dio|dire|dyer)\b(?=[^.?!]*$)", "in Dia")]
WEBBY = re.compile(r"\b(open|page|tab|site|website|wikipedia|dashboard|browser|google|url|link)\b", re.I)

lock = threading.Lock()  # MLX models aren't safe to call from two threads at once
FAKE = os.environ.get("JARVIS_VOICE_FAKE") == "1"
if not FAKE:
    try:
        import mlx_whisper
        from mlx_audio.tts.utils import load_model
        tts_model = load_model(KOKORO)
    except Exception as e:  # noqa: BLE001
        print(f"on-device voice unavailable ({e}); using test answers")
        FAKE = True


def fix(text: str) -> str:
    for pat, rep in FIXES:
        if rep == "in Dia" and not WEBBY.search(text):
            continue
        text = re.sub(pat, rep, text, flags=re.I)
    return text.strip()


def stt(audio: bytes, ext: str) -> str:
    if FAKE:
        return "What's on my calendar today?"
    with tempfile.NamedTemporaryFile(suffix="." + ext) as f:
        f.write(audio)
        f.flush()
        with lock:
            r = mlx_whisper.transcribe(f.name, path_or_hf_repo=WHISPER, initial_prompt=HINT, language="en")
    return fix(r.get("text") or "")


def tts(text: str, speed: float = 1.2) -> np.ndarray:
    if FAKE:
        return np.zeros(int(RATE * min(3.0, 0.06 * len(text) + 0.3)), dtype=np.int16)
    with lock:
        parts = [np.asarray(r.audio, dtype=np.float32) for r in tts_model.generate(
            text=text, voice=VOICE, lang_code=VOICE[0], speed=speed)]
    a = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return (np.clip(a, -1, 1) * 32767).astype(np.int16)


def to_wav(pcm: np.ndarray) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return b.getvalue()


EXT = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a", "audio/aac": "m4a", "audio/mpeg": "mp3",
       "audio/ogg": "ogg", "audio/webm": "webm"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, json.dumps({"ok": True, "fake": FAKE, "voice": VOICE}).encode(), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            if self.path.startswith("/stt"):
                ext = EXT.get((self.headers.get("Content-Type") or "").split(";")[0].strip(), "webm")
                self._send(200, json.dumps({"text": stt(body, ext)}).encode(), "application/json")
            elif self.path.startswith("/tts"):
                req = json.loads(body or b"{}")
                pcm = tts(str(req.get("text") or "")[:1500], max(0.8, min(1.6, float(req.get("speed") or 1.2))))
                if "wav=1" in self.path:
                    self._send(200, to_wav(pcm), "audio/wav")
                else:
                    self._send(200, pcm.tobytes(), "application/octet-stream")
            else:
                self._send(404, b"{}", "application/json")
        except Exception as e:  # noqa: BLE001
            print(f"{self.path}: {str(e)[:300]}")
            self._send(500, json.dumps({"error": str(e)[:200]}).encode(), "application/json")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    tts("Ready.")  # warm both up, so the first real request is quick
    if not FAKE:
        stt(to_wav(np.zeros(RATE // 2, np.int16)), "wav")
    print(f"on-device voice ready on 127.0.0.1:{PORT} (voice {VOICE}{', test mode' if FAKE else ''})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

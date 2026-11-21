"""Voice engine "On this Mac": hold Option + Command, Whisper writes down what you said, Jev or the
voice brain (a local model by default) does it, and Kokoro says the answer, all on this Mac.
The ears and voice run in local_voice_server.py (own venv), started here on first use."""

import asyncio
import io
import json
import os
import queue
import re
import subprocess
import time
import urllib.request
import wave
from contextlib import suppress
from pathlib import Path

import numpy as np
import sounddevice as sd

import copilot_voice as cv
import rules
import settings
from agents import Agent, ModelSettings
from openai.types.shared import Reasoning

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("JARVIS_VOICE_PORT", "8767"))
URL = f"http://127.0.0.1:{PORT}"
ACK_AFTER_S = 1.5  # say "Yes, my lord." if the answer takes longer than this
_proc: subprocess.Popen | None = None

brain = Agent(
    name="Mac Operator (voice)",
    tools=cv.ALL_TOOLS,
    instructions=rules.instructions(cv.INSTRUCTIONS
    + "\nThe user is talking to you by voice and hears your reply read aloud. Before anything that "
    "sends, posts or replies as the user (texts, Slack, email), say once where it goes and the exact "
    "words, then ask 'Send it?'. If the user's next words are yes (or 'send it'), that is the "
    "confirmation: send exactly that, without asking again. If one contact matches the name, use it. "
    "Reply in one or two short, plain sentences, with no lists or formatting."),
    model_settings=ModelSettings(reasoning=Reasoning(effort="low")),  # gpt-oss answers ~3x sooner
)


def _call(path: str, data: bytes | None = None, ctype: str = "application/json", timeout: float = 60) -> bytes:
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _alive() -> bool:
    try:
        return json.loads(_call("/health", timeout=1)).get("ok", False)
    except (OSError, ValueError):
        return False


async def up(wait_s: float = 120) -> bool:
    """Starts the ears and voice if they aren't running, and waits until they're warm."""
    global _proc
    if await asyncio.to_thread(_alive):
        return True
    py = ROOT / ".venv-voice" / "bin" / "python"
    if not py.exists():
        raise RuntimeError("on-device voice isn't installed (.venv-voice is missing)")
    if not (_proc and _proc.poll() is None):
        (ROOT / "logs").mkdir(exist_ok=True)
        env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")}
        _proc = subprocess.Popen([str(py), str(ROOT / "local_voice_server.py")], cwd=ROOT, env=env,
                                 stdout=open(ROOT / "logs" / "voice_server.log", "a"), stderr=subprocess.STDOUT)
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if await asyncio.to_thread(_alive):
            return True
        if _proc.poll() is not None:
            break
        await asyncio.sleep(0.5)
    return False


async def hear(audio: bytes, ctype: str = "audio/wav") -> str:
    out = await asyncio.to_thread(_call, "/stt", audio, ctype)
    return str(json.loads(out).get("text") or "").strip()


def _say(text: str) -> bytes:
    return json.dumps({"text": text, "speed": settings.get("voice_speed")}).encode()


async def say_pcm(text: str) -> np.ndarray:
    out = await asyncio.to_thread(_call, "/tts", _say(text))
    return np.frombuffer(out, dtype=np.int16)


async def say_wav(text: str) -> bytes:
    return await asyncio.to_thread(_call, "/tts?wav=1", _say(text))


def wav(pcm: np.ndarray, rate: int = cv.SAMPLE_RATE) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return b.getvalue()


def sentences(text: str) -> list[str]:
    text = re.sub(r"[*_`#>]+", "", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


class LocalVoice(cv.VoiceCopilot):
    """Same controls as the OpenAI voice (hold to talk, Space, mute), with no live connection."""

    prebuffer_chunks = 1  # each chunk is a whole spoken sentence, so play it at once

    def __init__(self, prebuffer: bool = False) -> None:
        super().__init__(prebuffer)
        self.turn = 0
        self.ack: np.ndarray | None = None

    async def run(self) -> None:
        print("Starting on-device voice...")
        if not await up():
            raise RuntimeError("on-device voice didn't start (see logs/voice_server.log)")
        with suppress(Exception):
            self.ack = await say_pcm("Yes, my lord.")
        asyncio.create_task(asyncio.to_thread(settings.keep_warm))
        self.audio_player = sd.OutputStream(
            channels=cv.CHANNELS, samplerate=cv.SAMPLE_RATE, dtype=cv.FORMAT,
            callback=self._output_callback, blocksize=int(cv.SAMPLE_RATE * cv.CHUNK_LENGTH_S))
        self.audio_player.start()
        self.audio_stream = sd.InputStream(
            channels=cv.CHANNELS, samplerate=cv.SAMPLE_RATE, dtype=cv.FORMAT,
            blocksize=int(cv.SAMPLE_RATE * cv.CHUNK_LENGTH_S),
            callback=lambda data, frames, t, status: self.mic_q.put(data.copy()))
        self.audio_stream.start()
        self.recording = True
        watcher = asyncio.create_task(self._watch_speaking())
        cv.emit(type="voice", state="ready")
        print("\n🎙️  Ready (on this Mac)! Hold Option + Command while you talk, let go when you're done.\n")
        try:
            await self.capture_audio()
            if self.mic_dead:
                raise RuntimeError("the microphone stopped sending audio, reconnecting")
        finally:
            self.recording = False
            watcher.cancel()
            if self.speaking:
                cv.emit(type="speaking", on=False)
            for s in (self.audio_player, self.audio_stream):
                with suppress(Exception):
                    s.stop()
                    s.close()

    async def capture_audio(self) -> None:
        backlog = self.mic_q.qsize() if self.prebuffer else 0
        while not self.prebuffer and not self.mic_q.empty():
            self.mic_q.get_nowait()
        talking, chunks, last = False, [], time.monotonic()
        while self.recording:
            try:
                data = self.mic_q.get_nowait()
            except queue.Empty:
                if time.monotonic() - last > 3:
                    print("⚠️  Microphone went quiet (no audio at all), reconnecting voice")
                    self.mic_dead = True
                    return
                await asyncio.sleep(0.01)
                continue
            last = time.monotonic()
            held = backlog > 0 or cv.talk_keys_held() or cv.ui_talk.is_set()
            backlog = max(0, backlog - 1)
            if held and not talking:
                talking, chunks = True, []
                self.turn += 1
                self.interrupt_event.set()
                self.hushed = False
                cv.emit(type="listening", on=True)
                print("🔴 Listening... (let go when done)")
            if talking and held:
                chunks.append(data[:, 0] if data.ndim > 1 else data)
                if len(chunks) % 3 == 0:
                    cv.emit(type="level", v=round(min(1.0, self._rms(data) * 6), 3))
            elif talking:
                talking = False
                enough = len(chunks) >= cv.MIN_TALK_CHUNKS
                cv.emit(type="listening", on=False, sent=enough)
                if enough:
                    asyncio.create_task(self.answer(np.concatenate(chunks), self.turn))
            await asyncio.sleep(0)

    async def answer(self, pcm: np.ndarray, turn: int) -> None:
        """One spoken request: write it down, do it, say the reply (unless the user talked again)."""
        try:
            text = await hear(wav(pcm))
        except Exception as e:  # noqa: BLE001
            cv.emit(type="voice", state="warn", text=f"Couldn't write that down: {str(e)[:200]}")
            return
        if turn != self.turn:
            return
        if not text:
            await self.speak("I didn't catch that.", turn)
            return
        cv.emit(type="heard", text=text)
        print(f"You: {text}")
        if await cv.stop_job():
            print("      ✋ Stopped the previous job.")
        job = cv._current_job = asyncio.create_task(cv._run_worker(text, brain, "brain"))
        try:
            reply = await asyncio.wait_for(asyncio.shield(job), ACK_AFTER_S)
        except asyncio.TimeoutError:
            if self.ack is not None and turn == self.turn and not (self.muted or self.hushed):
                self.output_queue.put_nowait((self.ack, "local", 0))
            try:
                reply = await job
            except (asyncio.CancelledError, Exception):
                return
        except (asyncio.CancelledError, Exception):
            return
        print(f"Copilot: {reply}\n")
        cv.emit(type="said", text=reply)
        await self.speak(reply, turn)

    async def speak(self, text: str, turn: int) -> None:
        for s in sentences(text):
            if turn != self.turn or self.muted or self.hushed:
                return
            try:
                pcm = await say_pcm(s)
            except Exception as e:  # noqa: BLE001
                print(f"voice: {str(e)[:200]}")
                return
            if turn != self.turn or self.muted or self.hushed:
                return
            self.output_queue.put_nowait((pcm, "local", 0))

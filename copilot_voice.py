"""Mac Copilot, voice mode: talk to it and it operates your Mac.
Based on the OpenAI Agents SDK realtime CLI example (gpt-realtime-2.1)."""

import asyncio
import ctypes
import ctypes.util
import queue
import sys
import threading
import time
from contextlib import suppress
from typing import Any, Callable

import numpy as np
import sounddevice as sd

from desktop_tools import load_env
import helpers
import settings
from helpers import MAIN_INSTRUCTIONS as INSTRUCTIONS, MAIN_TOOLS as ALL_TOOLS

load_env()

from agents import Agent, RunConfig, Runner, function_tool  # noqa: E402
import rules  # noqa: E402
from agents.items import ToolCallItem  # noqa: E402
from agents.realtime import (  # noqa: E402
    RealtimeAgent,
    RealtimePlaybackTracker,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)
from agents.realtime.model import RealtimeModelConfig  # noqa: E402
from agents.realtime.model_inputs import RealtimeModelSendRawMessage  # noqa: E402

from router import route_request  # noqa: E402
from runlog import RunLog  # noqa: E402

_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_cg.CGEventSourceFlagsState.restype = ctypes.c_uint64
_cg.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
_OPTION, _COMMAND = 1 << 19, 1 << 20
MIN_TALK_CHUNKS = 8  # ~0.3 s; shorter presses are ignored


def talk_keys_held() -> bool:
    """True while Option and Command are both held down (works in any app)."""
    flags = _cg.CGEventSourceFlagsState(0)
    return bool(flags & _OPTION and flags & _COMMAND)

CHUNK_LENGTH_S = 0.04
SAMPLE_RATE = 24000
FORMAT = np.int16
CHANNELS = 1
ENERGY_THRESHOLD = 0.015
PREBUFFER_CHUNKS = 3
PLAYBACK_ECHO_MARGIN = 0.002

worker = Agent(
    name="Mac Operator",
    model="gpt-5.5",
    tools=ALL_TOOLS,
    instructions=rules.instructions(INSTRUCTIONS
    + "\nTasks are relayed to you from a voice assistant; you cannot ask follow-up questions "
    "directly. If a task needs confirmation under the Safety rule and doesn't say the user "
    "confirmed (or said \"just send it\" / \"don't ask\"), don't do it: reply with exactly what needs confirming. "
    "Texts, Slack and email always go through send_text, slack_send and send_email (never the app "
    "windows), so a confirmed send takes one call. If one contact matches the name, use it without asking. "
    "Reply in one or two plain sentences that will be read aloud."),
)
_worker_history: list = []
_current_job: asyncio.Task | None = None

# The web page (copilot_web.py) plugs in here: emit() gets live events, ui_talk is its mic button.
emit: Callable[..., None] = lambda **event: None
ui_talk = threading.Event()


def set_emitter(fn: Callable[..., None]) -> None:
    global emit
    emit = fn


async def stop_job() -> bool:
    if _current_job and not _current_job.done():
        _current_job.cancel()
        with suppress(BaseException):
            await _current_job
        return True
    return False


def reset_history() -> None:
    global _worker_history
    _worker_history = []


async def _run_worker(task: str, agent: Agent | None = None, kind: str = "work") -> str:
    import desktop_tools as dt
    dt._ctx.set({"snap": ""})  # own snapshot ids, separate from typed tasks running alongside
    log = RunLog("voice", task)
    emit(type="task", text=task)
    try:
        reply = await _work(task, log, agent or worker, kind)
    except asyncio.CancelledError:
        log.finish("stopped")
        emit(type="stopped")
        emit(type="end")
        raise
    except Exception as e:
        log.finish("error", f"{type(e).__name__}: {str(e)[:300]}")
        emit(type="error", text=f"Something went wrong: {str(e)[:300]}")
        emit(type="end")
        raise
    log.finish("done", reply)
    emit(type="reply", text=reply)
    emit(type="end")
    return reply


async def _work(task: str, log: RunLog, agent: Agent, kind: str) -> str:
    global _worker_history
    quick = await route_request(task)
    if quick.handled:
        log.route("jev", quick.ms, quick.route)
        emit(type="route", via="jev", ms=quick.ms, route=quick.route)
        print(f"      ⚡ Jev ({quick.ms} ms): {quick.route}")
        _worker_history += [{"role": "user", "content": task},
                            {"role": "assistant", "content": quick.reply}]
        return quick.reply
    log.route(settings.label(kind), quick.ms)
    emit(type="route", via=settings.label(kind), ms=quick.ms)
    helpers.EMIT.set(lambda **event: emit(**event))
    agent.model = settings.agent_model(kind)
    from screen_tools import front_note
    ask = helpers.stamp(helpers.demo_note(task)) + await asyncio.to_thread(front_note)
    result = Runner.run_streamed(
        agent, _worker_history + [{"role": "user", "content": ask}], max_turns=40,
        run_config=RunConfig(tracing_disabled=settings.is_local(kind)),
    )
    try:
        async for ev in result.stream_events():
            if ev.type == "run_item_stream_event":
                log.item(ev.item)
            if ev.type == "run_item_stream_event" and isinstance(ev.item, ToolCallItem):
                raw = ev.item.raw_item
                emit(type="tool", name=getattr(raw, "name", "tool"),
                     args=getattr(raw, "arguments", ""))
                print(f"      ⚙️  {getattr(raw, 'name', 'tool')} {getattr(raw, 'arguments', '')}")
    except asyncio.CancelledError:
        result.cancel()
        raise
    _worker_history = result.to_input_list()
    return str(result.final_output)


@function_tool
async def do_on_mac(task: str) -> str:
    """Carry out a task on the user's Mac (open apps, websites, click, type, play music,
    quit apps, settings...). Describe the full task in plain words with every detail the user
    gave, using app names exactly as the user said them. Calling this again replaces any
    task still running. Only say the user confirmed a sensitive step if they said yes after
    you read back exactly what will be sent or posted, and where."""
    global _current_job
    if await stop_job():
        print("      ✋ Stopped the previous job.")
    job = asyncio.create_task(_run_worker(task))
    _current_job = job
    try:
        return await job
    except asyncio.CancelledError:
        return "(Replaced by a newer request. Say nothing about this.)"
    except Exception as e:
        return f"Something went wrong: {str(e)[:200]}"


agent = RealtimeAgent(
    name="Mac Copilot",
    instructions=(
        "You are Mac Copilot, a friendly voice assistant that can operate the user's Mac. "
        "Whenever the user wants something done on their Mac, say a quick 'on it', then call "
        "do_on_mac with a clear description of the whole task, and tell them the result. "
        "Use app and website names exactly as the user says them; never substitute another "
        "app (e.g. 'Dia' is a web browser, not Safari). "
        "If the user changes their mind, just call do_on_mac with the new task; it replaces "
        "the old one. Wait for do_on_mac's answer before saying whether it worked, and "
        "never claim something happened unless do_on_mac said so. If do_on_mac asks for "
        "confirmation, ask the user; only if they say yes, call it again saying they confirmed. "
        "Before anything that sends, posts or replies as the user (texts, Slack, email), read "
        "back once where it goes and the exact words and ask 'Send it?' (unless they said 'just send it' "
        "or 'don't ask': then tell do_on_mac that and don't ask). The user's yes (or "
        "'send it') after that read-back is the confirmation: call do_on_mac with the exact "
        "recipient and words, saying the user confirmed, and never ask about it again. "
        "Write drafts yourself; never ask do_on_mac to type a draft into an app. Don't ask the "
        "user to confirm a contact unless do_on_mac found more than one match. "
        "You are speaking out loud: keep it to one or two short, upbeat sentences."
    ),
    tools=[do_on_mac],
)


class VoiceCopilot:
    prebuffer_chunks = PREBUFFER_CHUNKS  # streamed audio arrives in small pieces: wait for a few first

    def __init__(self, prebuffer: bool = False) -> None:
        self.prebuffer = prebuffer  # keep what was said while connecting (voice turned on by holding to talk)
        self.mic_q: queue.Queue[np.ndarray] = queue.Queue()
        self.mic_dead = False  # the mic stopped sending audio (device changed or errored): reconnect
        self.session: RealtimeSession | None = None
        self.audio_stream: sd.InputStream | None = None
        self.audio_player: sd.OutputStream | None = None
        self.recording = False
        self.capture_task: asyncio.Task[None] | None = None
        self.playback_tracker = RealtimePlaybackTracker()
        self.output_queue: queue.Queue[Any] = queue.Queue(maxsize=0)
        self.interrupt_event = threading.Event()
        self.current_chunk: tuple[np.ndarray, str, int] | None = None
        self.chunk_pos = 0
        self.prebuffering = True
        self.playback_rms = 0.0
        self.reply_text = ""
        self.speaking = False
        self.muted = False  # drop Jarvis's audio, keep everything else
        self.hushed = False  # Space was pressed: stay quiet until the user talks again

    def hush(self) -> None:
        """Stops what Jarvis is saying right now; the conversation and any task carry on."""
        self.hushed = True
        self.interrupt_event.set()

    async def stop_speaking(self) -> None:
        self.interrupt_event.set()
        if self.session:
            with suppress(Exception):
                await self.session.interrupt()

    async def _watch_speaking(self) -> None:
        while True:
            now = self.current_chunk is not None or not self.output_queue.empty()
            if now != self.speaking:
                self.speaking = now
                emit(type="speaking", on=now)
            await asyncio.sleep(0.1)

    def _output_callback(self, outdata, frames: int, time, status) -> None:
        outdata.fill(0)
        if self.interrupt_event.is_set():
            self.current_chunk = None
            self.chunk_pos = 0
            while not self.output_queue.empty():
                with suppress(queue.Empty):
                    self.output_queue.get_nowait()
            self.prebuffering = True
            self.interrupt_event.clear()
            return
        filled = 0
        while filled < len(outdata):
            if self.current_chunk is None:
                if self.prebuffering and self.output_queue.qsize() < self.prebuffer_chunks:
                    break
                self.prebuffering = False
                try:
                    self.current_chunk = self.output_queue.get_nowait()
                    self.chunk_pos = 0
                except queue.Empty:
                    break
            samples, item_id, content_index = self.current_chunk
            n = min(len(outdata) - filled, len(samples) - self.chunk_pos)
            data = samples[self.chunk_pos : self.chunk_pos + n]
            outdata[filled : filled + n, 0] = data
            self._update_playback_rms(data)
            filled += n
            self.chunk_pos += n
            with suppress(Exception):
                self.playback_tracker.on_play_bytes(
                    item_id=item_id, item_content_index=content_index, bytes=data.tobytes()
                )
            if self.chunk_pos >= len(samples):
                self.current_chunk = None
                self.chunk_pos = 0

    async def run(self) -> None:
        print("Connecting to OpenAI...")
        self.audio_player = sd.OutputStream(
            channels=CHANNELS, samplerate=SAMPLE_RATE, dtype=FORMAT,
            callback=self._output_callback, blocksize=int(SAMPLE_RATE * CHUNK_LENGTH_S),
        )
        self.audio_player.start()
        # the mic opens before connecting, so words said while it connects aren't lost
        self.audio_stream = sd.InputStream(
            channels=CHANNELS, samplerate=SAMPLE_RATE, dtype=FORMAT,
            blocksize=int(SAMPLE_RATE * CHUNK_LENGTH_S),
            callback=lambda data, frames, t, status: self.mic_q.put(data.copy()),
        )
        self.audio_stream.start()
        try:
            config: RealtimeModelConfig = {
                "playback_tracker": self.playback_tracker,
                "initial_model_settings": {
                    "model_name": settings.get("model_voice"),
                    "voice": "ash",
                    "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": None,
                },
            }
            async with await RealtimeRunner(agent).run(model_config=config) as session:
                self.session = session
                self.recording = True
                self.capture_task = asyncio.create_task(self.capture_audio())
                watcher = asyncio.create_task(self._watch_speaking())
                emit(type="voice", state="ready")
                print(
                    "\n🎙️  Ready! Hold Option + Command while you talk, let go when you're done."
                    "\n    Works from any app. (Ctrl+C here to quit)\n"
                )
                try:
                    async for event in session:
                        await self._on_event(event)
                    if self.mic_dead:
                        raise RuntimeError("the microphone stopped sending audio, reconnecting")
                finally:
                    watcher.cancel()
                    if self.speaking:
                        emit(type="speaking", on=False)
        finally:
            self.recording = False
            if self.capture_task:
                self.capture_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.capture_task
            if self.audio_player:
                self.audio_player.stop()
                self.audio_player.close()
            with suppress(Exception):
                self.audio_stream.stop()
                self.audio_stream.close()

    async def capture_audio(self) -> None:
        assert self.audio_stream and self.session
        backlog = self.mic_q.qsize() if self.prebuffer else 0
        while not self.prebuffer and not self.mic_q.empty():
            self.mic_q.get_nowait()
        talking, sent, last = False, 0, time.monotonic()
        try:
            while self.recording:
                try:
                    data = self.mic_q.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last > 3:
                        print("⚠️  Microphone went quiet (no audio at all), reconnecting voice")
                        self.mic_dead = True
                        await self.session.close()
                        return
                    await asyncio.sleep(0.01)
                    continue
                last = time.monotonic()
                held = backlog > 0 or talk_keys_held() or ui_talk.is_set()
                backlog = max(0, backlog - 1)
                if held and not talking:
                    talking, sent = True, 0
                    self.interrupt_event.set()
                    with suppress(Exception):
                        await self.session.interrupt()
                    await self._raw({"type": "input_audio_buffer.clear"})
                    emit(type="listening", on=True)
                    self.hushed = False  # talking again: Jarvis may answer out loud
                    print("🔴 Listening... (let go when done)")
                if talking and held:
                    await self.session.send_audio(data.tobytes())
                    sent += 1
                    if sent % 3 == 0:
                        emit(type="level", v=round(min(1.0, self._rms(data) * 6), 3))
                elif talking:
                    talking = False
                    emit(type="listening", on=False, sent=sent >= MIN_TALK_CHUNKS)
                    if sent >= MIN_TALK_CHUNKS:
                        await self.session.send_audio(data.tobytes(), commit=True)
                        await self._raw({"type": "response.create"})
                    else:
                        await self._raw({"type": "input_audio_buffer.clear"})
                await asyncio.sleep(0)
        except Exception:
            pass

    async def _raw(self, message: dict) -> None:
        assert self.session
        await self.session.model.send_event(RealtimeModelSendRawMessage(message=message))

    async def _on_event(self, event: RealtimeSessionEvent) -> None:
        if event.type == "audio":
            if self.muted or self.hushed:
                return
            audio = np.frombuffer(event.audio.data, dtype=np.int16)
            self.output_queue.put_nowait((audio, event.item_id, event.content_index))
        elif event.type == "audio_interrupted":
            self.interrupt_event.set()
        elif event.type == "tool_start":
            print(f"   🧠 Working on: {event.arguments}")
        elif event.type == "raw_model_event":
            d = event.data
            if d.type == "input_audio_transcription_completed" and d.transcript.strip():
                emit(type="heard", text=d.transcript.strip())
                print(f"You: {d.transcript.strip()}")
            elif d.type == "transcript_delta":
                self.reply_text += d.delta
            elif d.type == "turn_ended" and self.reply_text.strip():
                emit(type="said", text=self.reply_text.strip())
                print(f"Copilot: {self.reply_text.strip()}\n")
                self.reply_text = ""
        elif event.type == "error":
            if "active response" not in str(event.error).lower():
                emit(type="voice", state="warn", text=str(event.error)[:300])
                print(f"Error: {event.error}")

    @staticmethod
    def _rms(samples: np.ndarray) -> float:
        if samples.size == 0:
            return 0.0
        x = samples.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(x * x)))

    def _update_playback_rms(self, samples: np.ndarray) -> None:
        self.playback_rms = 0.9 * self.playback_rms + 0.1 * self._rms(samples)


if __name__ == "__main__":
    try:
        asyncio.run(VoiceCopilot().run())
    except KeyboardInterrupt:
        print("\nBye!")
        sys.exit(0)

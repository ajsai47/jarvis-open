"""Connection to the Jarvis for Dia extension (dia-extension/). The extension dials in over a
WebSocket at /api/browser; Jarvis sends it commands ({id, cmd, args}) and awaits the replies."""
import asyncio
import itertools
import json
import time
from contextlib import suppress
from pathlib import Path

EXTENSION_ID = (Path(__file__).parent / "dia-extension" / "EXTENSION_ID").read_text().strip()
BROWSERS = {"Dia", "Safari", "Google Chrome", "Chrome", "Arc"}
CALL_TIMEOUT_S = 45
NOT_CONNECTED = ("Dia isn't connected (the Jarvis for Dia extension isn't running). "
                 "Fall back to open_website and look_at_app on Dia.")


class Bridge:
    def __init__(self) -> None:
        self.ws = None
        self.version = ""
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def call(self, cmd: str, **args):
        if not self.ws:
            raise RuntimeError(NOT_CONNECTED)
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        try:
            await self.ws.send_text(json.dumps({"id": i, "cmd": cmd, "args": args}))
            msg = await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        finally:
            self.pending.pop(i, None)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error") or "Dia couldn't do that.")
        return msg.get("result")

    async def serve(self, ws) -> None:
        old, self.ws = self.ws, ws  # newest connection wins
        if old:
            with suppress(Exception):
                await old.close()

        async def ping():
            while True:
                await asyncio.sleep(20)
                await ws.send_text(json.dumps({"ping": int(time.time())}))

        pinger = asyncio.create_task(ping())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if "hello" in msg:
                    self.version = str(msg.get("version", ""))
                    continue
                fut = self.pending.get(msg.get("id"))
                if fut and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            pinger.cancel()
            if self.ws is ws:
                self.ws = None
                for f in self.pending.values():
                    if not f.done():
                        f.set_exception(RuntimeError("Dia disconnected."))


BRIDGE = Bridge()

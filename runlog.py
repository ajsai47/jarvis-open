"""Saves every run to logs/runs.jsonl on this Mac (git-ignored), so feedback like
"the last run did X" can be checked against exactly what happened."""

import json
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "runs.jsonl"
KEEP = 300
MAX_OUTPUT = 8000


class RunLog:
    def __init__(self, mode: str, request: str) -> None:
        self.t0 = time.monotonic()
        self.run = {"id": f"{time.time():.3f}", "time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
                    "request": request, "route": None, "steps": [], "reply": None,
                    "status": "running"}

    def _t(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def route(self, via: str, ms: int, route: str | None = None) -> None:
        self.run["route"] = {"via": via, "jev_ms": ms, "action": route}

    def tool_call(self, name: str, args: str) -> None:
        self.run["steps"].append({"t": self._t(), "tool": name, "args": args})

    def tool_output(self, output) -> None:
        for step in reversed(self.run["steps"]):
            if "result" not in step:
                step["result"] = str(output)[:MAX_OUTPUT]
                return

    def item(self, item) -> None:
        """Record a streamed Agents SDK run item (tool calls and their results)."""
        from agents.items import ToolCallItem, ToolCallOutputItem
        if isinstance(item, ToolCallItem):
            raw = item.raw_item
            self.tool_call(getattr(raw, "name", "tool"), getattr(raw, "arguments", ""))
        elif isinstance(item, ToolCallOutputItem):
            self.tool_output(item.output)

    def finish(self, status: str, reply: str | None = None) -> None:
        self.run.update(status=status, reply=reply, seconds=self._t())
        try:
            LOG_DIR.mkdir(mode=0o700, exist_ok=True)
            lines = LOG_FILE.read_text().splitlines()[-(KEEP - 1):] if LOG_FILE.exists() else []
            lines.append(json.dumps(self.run, ensure_ascii=False))
            LOG_FILE.write_text("\n".join(lines) + "\n")
            LOG_FILE.chmod(0o600)
        except OSError:
            pass

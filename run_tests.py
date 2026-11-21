"""Run a list of read-only test tasks through Mac Copilot and log every step."""

import asyncio
import sys
import time

from desktop_tools import ALL_TOOLS, INSTRUCTIONS, load_env

load_env()

from agents import Agent, Runner  # noqa: E402
from agents.items import ToolCallItem, ToolCallOutputItem  # noqa: E402

TEST_RULE = (
    "\nThis is a read-only test run: never send, post, like, react, follow, delete or edit "
    "anything. Only open, look, scroll, search and read."
)
agent = Agent(name="Mac Copilot", instructions=INSTRUCTIONS + TEST_RULE, tools=ALL_TOOLS,
              model="gpt-5.5")


async def run(task: str) -> None:
    print(f"\n=== TASK: {task}")
    t0 = time.time()
    result = Runner.run_streamed(agent, task, max_turns=40)
    try:
        async for ev in result.stream_events():
            if ev.type != "run_item_stream_event":
                continue
            if isinstance(ev.item, ToolCallItem):
                raw = ev.item.raw_item
                print(f"  > {getattr(raw, 'name', 'tool')} {getattr(raw, 'arguments', '')}")
            elif isinstance(ev.item, ToolCallOutputItem):
                out = str(ev.item.output).replace("\n", " | ")
                print(f"    < {out[:160]}")
    except Exception as e:
        print(f"  !! ERROR: {str(e)[:300]}")
        return
    print(f"=== RESULT ({time.time() - t0:.0f}s): {result.final_output}")


async def main() -> None:
    for task in sys.argv[1:]:
        await run(task)


if __name__ == "__main__":
    asyncio.run(main())

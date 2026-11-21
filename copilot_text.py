"""Mac Copilot, typing mode: chat in Terminal and the agent operates your Mac."""

import asyncio
import sys

from desktop_tools import load_env
from helpers import MAIN_INSTRUCTIONS as INSTRUCTIONS, MAIN_TOOLS as ALL_TOOLS

load_env()

from agents import Agent, Runner  # noqa: E402
from agents.items import ToolCallItem  # noqa: E402

from router import route_request  # noqa: E402
from runlog import RunLog  # noqa: E402

agent = Agent(name="Mac Copilot", instructions=INSTRUCTIONS, tools=ALL_TOOLS, model="gpt-5.5")


async def main() -> None:
    print("Mac Copilot (typing mode). Tell me what to do on your Mac. Type 'quit' to exit.\n")
    history: list = []
    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if msg.lower() in {"quit", "exit", "q"}:
            break
        if not msg:
            continue
        log = RunLog("text", msg)
        quick = await route_request(msg)
        if quick.handled:
            log.route("jev", quick.ms, quick.route)
            log.finish("done", quick.reply)
            print(f"   ⚡ Jev ({quick.ms} ms): {quick.route}\n\nCopilot: {quick.reply}\n")
            history += [{"role": "user", "content": msg},
                        {"role": "assistant", "content": quick.reply}]
            continue
        log.route("gpt-5.5", quick.ms)
        result = Runner.run_streamed(agent, history + [{"role": "user", "content": msg}], max_turns=40)
        try:
            async for ev in result.stream_events():
                if ev.type == "run_item_stream_event":
                    log.item(ev.item)
                if ev.type == "run_item_stream_event" and isinstance(ev.item, ToolCallItem):
                    raw = ev.item.raw_item
                    name = getattr(raw, "name", "tool")
                    args = getattr(raw, "arguments", "")
                    print(f"   ⚙️  {name} {args}")
        except Exception as e:
            print(f"\n⚠️  OpenAI error: {str(e)[:300]}\n")
            log.finish("error", f"{type(e).__name__}: {str(e)[:300]}")
            continue
        log.finish("done", str(result.final_output))
        print(f"\nCopilot: {result.final_output}\n")
        history = result.to_input_list()
    print("Bye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

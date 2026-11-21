"""Check which route Jev picks for a mix of requests (dry run: nothing is executed)."""

import asyncio

from desktop_tools import load_env

load_env()

from router import route_request  # noqa: E402

CASES = [
    ("Turn the volume down to 30", True),
    ("Mute the sound", True),
    ("Turn on dark mode", True),
    ("Switch to light mode please", True),
    ("Open Calculator", True),
    ("Launch Notes", True),
    ("Open Apple Music", True),
    ("Quit Music", True),
    ("Close Slack", True),
    ("Go to YouTube in Dia", True),
    ("Open spotify.com", True),
    ("Open TikTok", True),
    ("Summarize the last 3 messages in team-sf on Slack", False),
    ("Play some chill music", False),
    ("What's 12 times 12 on the calculator?", False),
    ("Send Taylor a Slack message saying I'm running late", False),
    ("Delete my latest note", False),
    ("Open Notes and write a to-do list for Friday", False),
    ("Quit Music and open YouTube in Dia", False),
    ("Make it louder", False),
]


async def main() -> None:
    right = 0
    for text, want_fast in CASES:
        r = await route_request(text, execute=False)
        ok = r.handled == want_fast
        right += ok
        how = f"FAST  {r.route}" if r.handled else f"LLM   (Jev said {r.route} @ {r.confidence:.2f})"
        print(f"{'ok ' if ok else 'XX '} {r.ms:4d} ms  {text!r:60} -> {how}")
    print(f"\n{right}/{len(CASES)} routed as expected")


asyncio.run(main())

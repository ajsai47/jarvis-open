"""Benchmark the Jev router.

Part 1: 100 requests, routing only (nothing runs on the Mac). Checks Jev's choice and speed.
Part 2: harmless requests (open/quit small apps) run for real, with Jev and with gpt-5.5 only.
"""

import asyncio
import json
import subprocess
import sys
import time
from collections import Counter

from desktop_tools import ALL_TOOLS, INSTRUCTIONS, load_env

load_env()

from agents import Agent, Runner  # noqa: E402

from router import route_request  # noqa: E402

# (request, expected). Expected is "llm" or "<action> <key arg>".
FAST = [
    ("Turn the volume down to 30", "set_volume 30"), ("Set volume to 50%", "set_volume 50"),
    ("volume 80", "set_volume 80"), ("Mute the sound", "set_volume 0"),
    ("mute", "set_volume 0"), ("Put the volume at 10 percent", "set_volume 10"),
    ("Can you set my sound to 65", "set_volume 65"), ("Turn volume all the way to 100", "set_volume 100"),
    ("Turn on dark mode", "set_dark_mode true"), ("Dark mode please", "set_dark_mode true"),
    ("Switch my Mac to dark mode", "set_dark_mode true"), ("enable dark appearance", "set_dark_mode true"),
    ("Switch to light mode please", "set_dark_mode false"), ("Turn off dark mode", "set_dark_mode false"),
    ("go back to light mode", "set_dark_mode false"),
    ("Open Calculator", "open_app Calculator"), ("Launch Notes", "open_app Notes"),
    ("Open Apple Music", "open_app Music"), ("open slack", "open_app Slack"),
    ("Start up Calendar", "open_app Calendar"), ("Open the Reminders app", "open_app Reminders"),
    ("Can you open Finder", "open_app Finder"), ("Open Dia", "open_app Dia"),
    ("Bring up System Settings", "open_app System Settings"), ("open maps", "open_app Maps"),
    ("Open TextEdit", "open_app TextEdit"), ("Fire up ChatGPT", "open_app ChatGPT"),
    ("Quit Music", "quit_app Music"), ("Close Slack", "quit_app Slack"),
    ("Quit Calculator", "quit_app Calculator"), ("quit notes", "quit_app Notes"),
    ("Shut down the Maps app", "quit_app Maps"), ("Exit Calendar", "quit_app Calendar"),
    ("Close TextEdit", "quit_app TextEdit"),
    ("Go to YouTube in Dia", "open_website youtube.com"), ("Open spotify.com", "open_website spotify.com"),
    ("Open TikTok", "open_website tiktok.com"), ("Take me to reddit", "open_website reddit.com"),
    ("open github.com", "open_website github.com"), ("Go to linkedin", "open_website linkedin.com"),
    ("Open Gmail", "open_website mail.google.com"), ("pull up netflix", "open_website netflix.com"),
    ("Open x.com in Dia", "open_website x.com"), ("Go to example.invalid", "open_website example.invalid"),
    ("Open Instagram in Safari", "open_website instagram.com"),
]
LLM = [
    "Summarize the last 3 messages in team-sf on Slack", "Play some chill music",
    "What's 12 times 12 on the calculator?", "Send Taylor a Slack message saying I'm running late",
    "Delete my latest note", "Open Notes and write a to-do list for Friday",
    "Quit Music and open YouTube in Dia", "Make it louder", "Turn it down a bit",
    "Who messaged me last on Slack?", "Skip this song", "Pause the music",
    "What song is playing?", "Read me my latest note", "Add a reminder to call Ade tomorrow",
    "Create a calendar event for lunch on Friday at noon", "Search YouTube for lo-fi beats",
    "Open Slack and go to team-sf", "Reply to Vik saying Jan 14 works",
    "Post on LinkedIn about our next event", "Buy the AirPods in my Amazon cart",
    "Empty the trash", "Change my Mac password", "Turn off the firewall",
    "What apps are open right now?", "Is Slack open?", "How many unread DMs do I have?",
    "Find the Apify prospects CSV on my Desktop", "Open the prospects spreadsheet in Numbers",
    "Calculate 23 times 7", "Play Apple Music Chill radio and turn on dark mode",
    "Close all my apps except Slack", "Open Notes and Calculator", "Mute and turn on dark mode",
    "Set a timer for 10 minutes", "What's the weather?", "Email Lisa the agenda",
    "Like the latest post on X", "Scroll my X feed and tell me the top 3 posts",
    "Open TikTok and scroll 5 videos", "Mark all Slack messages as read",
    "Write a haiku in TextEdit", "Copy the last Slack message into a new note",
    "Make the screen brighter", "Turn on Do Not Disturb", "Connect to my AirPods",
    "What's on my calendar today?", "Rename the file on my Desktop to final.csv",
    "Go to the next track and turn it up", "Play my workout playlist on Spotify",
    "Open the last thing I was working on", "Tell me a joke", "Thanks!",
    "Undo that", "Do the same thing again",
]
CASES = FAST + [(t, "llm") for t in LLM]
LIVE_APPS = ["Calculator", "Stickies", "Chess", "Dictionary", "TextEdit"]


def _matches(r, expected: str) -> bool:
    if expected == "llm":
        return not r.handled
    if not r.handled:
        return False
    name, args = r.route.split(" ", 1)
    want_name, want_arg = expected.split(" ", 1)
    vals = [str(v).lower() for v in json.loads(args).values()]
    return name == want_name and any(want_arg.lower() in v for v in vals)


async def part1() -> None:
    print(f"=== PART 1: {len(CASES)} requests, routing only (nothing runs)\n")
    wrong, times, kinds = [], [], Counter()
    for text, expected in CASES:
        r = await route_request(text, execute=False)
        times.append(r.ms)
        ok = _matches(r, expected)
        exp_fast = expected != "llm"
        kinds[("fast" if exp_fast else "llm", "right" if ok else ("slow" if exp_fast and not r.handled else "WRONG"))] += 1
        if not ok:
            got = r.route if r.handled else f"gpt-5.5 (Jev said {r.route} @ {r.confidence:.2f})"
            wrong.append(f"   {text!r:55} expected {expected:28} got {got}")
    times.sort()
    print(f"Correct: {kinds['fast','right'] + kinds['llm','right']}/{len(CASES)}")
    print(f"  Simple requests run instantly: {kinds['fast','right']}/{len(FAST)}"
          f"  (safely sent to gpt-5.5 instead: {kinds['fast','slow']}, wrong action: {kinds['fast','WRONG']})")
    print(f"  Complex/sensitive requests sent to gpt-5.5: {kinds['llm','right']}/{len(LLM)}"
          f"  (wrongly run instantly: {kinds['llm','WRONG']})")
    print(f"Jev time: median {times[len(times)//2]} ms, slowest {times[-1]} ms")
    print("Misses:\n" + ("\n".join(wrong) or "   none"))


def _running(app: str) -> bool:
    out = subprocess.run(["osascript", "-e", f'application "{app}" is running'],
                         capture_output=True, text=True).stdout.strip()
    return out == "true"


async def part2() -> None:
    apps = [a for a in LIVE_APPS if not _running(a)]
    print(f"\n=== PART 2: live, with vs without Jev ({', '.join(apps)})\n")
    agent = Agent(name="Mac Copilot", instructions=INSTRUCTIONS, tools=ALL_TOOLS, model="gpt-5.5")
    totals = {"jev": [], "gpt": []}
    for app in apps:
        for verb in ("Open", "Quit"):
            task = f"{verb} {app}"
            t0 = time.time()
            r = await route_request(task)
            jev_s = time.time() - t0
            if not r.handled:  # router passed it on; time the LLM like a real run
                await Runner.run(agent, task, max_turns=20)
                jev_s = time.time() - t0
            await asyncio.sleep(1)
            if verb == "Open":  # reset so gpt-5.5 starts from the same state
                await route_request(f"Quit {app}")
            else:
                await route_request(f"Open {app}")
            await asyncio.sleep(1)
            t0 = time.time()
            res = await Runner.run(agent, task, max_turns=20)
            gpt_s = time.time() - t0
            totals["jev"].append(jev_s)
            totals["gpt"].append(gpt_s)
            print(f"{task:20} with Jev {jev_s:5.1f}s ({'instant' if r.handled else 'gpt-5.5'})"
                  f"   without {gpt_s:5.1f}s   -> {str(res.final_output)[:60]!r}")
        if _running(app):
            await route_request(f"Quit {app}")
    j, g = sum(totals["jev"]), sum(totals["gpt"])
    print(f"\nTotal: with Jev {j:.1f}s, without {g:.1f}s ({g / max(j, 0.01):.1f}x faster with Jev)")


async def main() -> None:
    await part1()
    if "--live" in sys.argv:
        await part2()
        await asyncio.sleep(3.5)  # let the background focus watchers finish


asyncio.run(main())

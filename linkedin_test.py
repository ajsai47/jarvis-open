"""Offline checks for the LinkedIn helpers: screen detection, result parsing, picking, states, sheet I/O. No phone."""

import csv
import tempfile
import time
from pathlib import Path

import linkedin_sheet
import phone_tools as p


def _xml(*els: tuple[str, str, int]) -> str:
    body = "".join(f'<XCUIElementType{k} label="{lab}" y="{y}" height="40" visible="true"/>' for k, lab, y in els)
    return f'<XCUIElementTypeApplication label="LinkedIn">{body}</XCUIElementTypeApplication>'


def _fake(xml: str):
    return lambda method, path, body=None, timeout=20: {"value": xml}


SCREENS = [
    ("home", [("Button", "Menu", 62), ("Button", "Search", 62), ("Button", "Messaging", 56), ("Button", "Home", 874)]),
    ("tab", [("Button", "For you, 1 of 2, selected, For you", 73), ("Button", "Home", 874), ("Button", "Jobs", 874)]),
    ("tab", [("Button", "Describe the job you want", 62), ("Button", "Home", 874)]),
    ("suggestions", [("Button", "Back", 66), ("TextField", "", 66), ("Button", "Clear text", 74), ("Key", "q", 662)]),
    ("results", [("StaticText", "Search", 56), ("Button", "Back", 62), ("Button", "People", 100), ("Button", "Home", 874)]),
    ("menu", [("Button", "View profile", 62), ("Button", "Close menu", 0)]),
    ("page", [("Button", "Back", 56), ("SearchField", "Search messages", 57)]),
    ("profile", [("Button", "Back button", 56), ("Button", "Follow Nia Example", 457), ("Button", "More", 457),
                 ("Button", "Home", 874)]),
    ("sheet", [("Other", "dismiss popup", -956), ("Button", "Invite Nia Example to connect, Connect", 694),
               ("Button", "Back button", 56), ("Button", "More", 457)]),
    ("unknown", [("StaticText", "Something else", 300)]),
    ("alert: Some iCloud Data Isn’t Syncing", [("Alert", "Some iCloud Data Isn’t Syncing", 368), ("Button", "Home", 874)]),
]


def test_screens() -> None:
    real = p._wda
    try:
        for want, els in SCREENS:
            p._wda = _fake(_xml(*els))
            got = p._li_screen("sid")
            assert got == want, (want, got, els)
    finally:
        p._wda = real


def test_rows() -> None:
    nb = "\u00a0"
    xml = ('<XCUIElementTypeApplication>'
           f'<XCUIElementTypeButton label="Ada Example{nb}Premium, Ada Example{nb}Premium • 1st, AI security | Example Security" y="228" height="104" visible="true"/>'
           '<XCUIElementTypeButton label="Send a message to Ada Example" y="228" height="49" visible="true"/>'
           '<XCUIElementTypeButton label="Ada Kay Example • 2nd, Enterprise Account Executive" y="356" height="87" visible="true"/>'
           '<XCUIElementTypeButton label="Invite Ada Kay Example to connect" y="356" height="49" visible="true"/>'
           '<XCUIElementTypeButton label="Sam Example • 3rd+, Example Software" y="460" height="87" visible="true"/>'
           '<XCUIElementTypeButton label="Pending, click to withdraw invitation sent to Sam Example" y="470" height="49" visible="true"/>'
           '<XCUIElementTypeButton label="Nia Example Premium • 2nd, GTM Executive" y="560" height="104" visible="true"/>'
           '<XCUIElementTypeButton label="Follow Nia Example" y="560" height="49" visible="true"/>'
           '<XCUIElementTypeButton label="Ghost Row • 2nd, hidden" y="600" height="87" visible="false"/>'
           '</XCUIElementTypeApplication>')
    real = p._wda
    try:
        p._wda = _fake(xml)
        rows = p._li_rows("sid")
    finally:
        p._wda = real
    assert [r["degree"] for r in rows] == ["1st", "2nd", "3rd+", "2nd"], rows
    assert nb not in rows[0]["label"]
    assert [p._li_state(r) for r in rows] == ["connected", "can connect", "pending", "follow only"]
    assert p._li_pick(rows, "Ada  Example", "Example Security")[0] is rows[0]


def test_pick() -> None:
    def r(label, degree="2nd", act=""):
        return {"label": label, "degree": degree, "act": act}
    two = [r("Jordan Sample • 2nd, CEO at ExampleAI"), r("Jordan Sample • 3rd+, ExampleAI alum")]
    assert p._li_pick(two, "Jordan Sample", "ExampleAI")[0] is None  # same name, both mention the company
    assert "check by hand" in p._li_pick(two, "Jordan Sample", "ExampleAI")[1]
    assert "add their company" in p._li_pick(two, "Jordan Sample", "")[1]
    assert "none mentioning" in p._li_pick(two, "Jordan Sample", "Example Labs")[1]
    assert "no one called" in p._li_pick(two, "Nick Example", "")[1]
    assert p._li_pick([r("Jordan Sampleson • 2nd")], "Jordan Sample", "")[0] is None  # prefix isn't a match
    assert p._li_state(r("x", act="")) == "no Connect button"


def test_sheet() -> None:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "list.csv"
        with f.open("w", newline="") as fh:
            csv.writer(fh).writerows([["Name", "Company", "connection_note", "sent"],
                                      ["Ada Example", "Example Security", "hi there", ""],
                                      ["", "", "", ""],
                                      ["Sam Example", "Example Software", "", "requested 2026-11-06"]])
        path, rows = linkedin_sheet.read(str(f))
        assert [(r["name"], r["_row"]) for r in rows] == [("Ada Example", 2), ("Sam Example", 4)]
        assert [r["name"] for r in rows if not r.get("sent")] == ["Ada Example"]  # what the batch would pick
        linkedin_sheet.write(path, {2: {"linkedin_status": "connected"}})
        assert (Path(d) / "list.orig.csv").exists()
        _, rows = linkedin_sheet.read(str(f))
        assert rows[0]["linkedin_status"] == "connected" and rows[1].get("linkedin_status", "") == ""
        import openpyxl
        x = Path(d) / "list.xlsx"
        wb = openpyxl.Workbook()
        wb.active.append(["name", "company", "sent"])
        wb.active.append(["Nick Example", "Example Labs", None])
        wb.save(x)
        linkedin_sheet.write(x, {2: {"sent": "requested now", "linkedin_status": "pending"}})
        _, rows = linkedin_sheet.read(str(x))
        assert rows[0]["sent"] == "requested now" and rows[0]["linkedin_status"] == "pending"
        assert (Path(d) / "list.orig.xlsx").exists()


def test_no_note() -> None:
    import inspect
    for fn in (p._li_connect, p._li_connect_profile, p._li_after_invite, p._li_leave_profile):
        src = inspect.getsource(fn)
        assert "/value" not in src and "/wda/keys" not in src, "connect must never type anything (no notes)"
        assert "Personalize" not in src.replace('never "Personalize invite"', ""), "never the note option"


def test_abort() -> None:
    """A phone run whose caller timed out must not tap anything afterwards."""
    import asyncio
    taps = []
    old = p._wda, p._session
    p._wda = lambda method, path, body=None, timeout=20: taps.append(path) or {"value": {}}
    p._session = lambda fresh=False: "s"

    def slow(sid):
        time.sleep(0.5)
        p._click(sid, "x")
        return "tapped"

    async def main():
        try:
            await asyncio.wait_for(p._go(slow), 0.1)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1)
    try:
        asyncio.run(main())
        assert not any(x.endswith("/click") for x in taps), taps
        assert p.linkedin_connect.timeout_seconds >= 180 and p.linkedin_batch.timeout_seconds >= 3600
    finally:
        p._wda, p._session = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

"""Fill in public web forms in a TinyFish cloud browser (about $0.002 a minute), so nothing opens on the Mac.
Jarvis reads the form's fields, fills them, and shows the user the filled-in values. The Submit click only goes
through after his yes (decisions.gate, the same check as sends). Signed-in forms stay in Dia (web_* tools)."""
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from agents import function_tool
from pydantic import BaseModel

import decisions

BROWSER_URL = "https://api.browser.tinyfish.ai"
IDLE_S = 600
SUBMITS = re.compile(r"\b(submit|send|rsvp|register|sign ?up|book|buy|pay|order|confirm|join|apply|done|finish|reserve|subscribe)\b", re.I)
_S: dict = {}

FIELDS_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const label = e => {
    if (e.labels && e.labels.length) return clean(e.labels[0].innerText);
    const by = e.getAttribute('aria-labelledby');
    if (by) return clean(by.split(' ').map(i => document.getElementById(i)?.innerText || '').join(' '));
    if (e.getAttribute('aria-label')) return clean(e.getAttribute('aria-label'));
    const box = e.closest('[role=listitem], fieldset, .form-group, .field, label, li, tr, div');
    return clean(e.placeholder || box?.innerText || e.name || e.id);
  };
  const fields = [], buttons = [];
  document.querySelectorAll('input, textarea, select, [contenteditable=true]').forEach(e => {
    const t = (e.type || e.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(t) || !vis(e) || e.disabled) return;
    const ref = 'f' + fields.length; e.setAttribute('data-jv', ref);
    const f = {ref, type: e.tagName === 'SELECT' ? 'select' : e.isContentEditable ? 'text' : t, label: label(e),
               required: !!e.required || e.getAttribute('aria-required') === 'true'};
    if (f.type === 'select') { f.options = [...e.options].map(o => clean(o.text)).slice(0, 30); f.value = clean(e.selectedOptions[0]?.text); }
    else if (t === 'checkbox' || t === 'radio') { f.checked = e.checked; f.option = clean(e.labels?.[0]?.innerText || e.value); }
    else f.value = clean(e.value ?? e.innerText);
    fields.push(f);
  });
  document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], [role=radio], [role=checkbox]').forEach(e => {
    if (!vis(e) || e.closest('[data-jv]') === e && e.matches('input:not([type=submit]):not([type=button])')) return;
    const role = e.getAttribute('role');
    const text = clean(e.innerText || e.value || e.getAttribute('aria-label'));
    if (!text && !role) return;
    if (role === 'radio' || role === 'checkbox') {
      const ref = 'f' + fields.length; e.setAttribute('data-jv', ref);
      fields.push({ref, type: role, label: label(e.closest('[role=radiogroup], [role=list], [role=listitem]') || e), option: text,
                   checked: e.getAttribute('aria-checked') === 'true'});
      return;
    }
    const ref = 'b' + buttons.length; e.setAttribute('data-jv', ref);
    buttons.push({ref, text});
  });
  return {title: document.title, url: location.href, fields, buttons: buttons.slice(0, 15)};
}"""


def _api(method: str, url: str, body: dict | None = None, timeout: int = 90) -> dict:
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        raise RuntimeError("Cloud forms aren't set up: add TINYFISH_API_KEY to mac-copilot/.env.")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("TinyFish rejected the key (TINYFISH_API_KEY in .env).")
        if e.code == 402 or b"wallet" in (e.read() or b"").lower():
            raise RuntimeError("The TinyFish wallet is empty: top it up at agent.tinyfish.ai, or use the web_* tools in Dia.")
        if e.code == 429:
            raise RuntimeError("TinyFish is busy (too many cloud browsers). Try again in a minute.")
        raise RuntimeError(f"TinyFish browser error {e.code}.")


async def close() -> None:
    s = dict(_S)
    _S.clear()
    for step in (lambda: s["browser"].close(), lambda: s["pw"].stop()):
        try:
            await step()
        except Exception:
            pass
    if s.get("id"):
        try:
            await asyncio.to_thread(_api, "DELETE", f"{BROWSER_URL}/{s['id']}", None, 30)
        except Exception:
            pass


async def close_idle() -> None:
    if _S and time.time() - _S.get("at", 0) > IDLE_S:
        await close()


def _page():
    if not _S:
        raise RuntimeError("No form is open. Call form_open first.")
    _S["at"] = time.time()
    return _S["page"]


def _show(form: dict) -> str:
    lines = [f"Form: {form['title']} ({form['url']})"]
    for f in form["fields"]:
        if f["type"] in ("checkbox", "radio"):
            lines.append(f"{f['ref']} [{f['type']}{', required' if f.get('required') else ''}] {f['label']} → "
                         f"{f['option']}{' (checked)' if f.get('checked') else ''}")
        else:
            opts = f" options: {' | '.join(f['options'])}" if f.get("options") else ""
            lines.append(f"{f['ref']} [{f['type']}{', required' if f.get('required') else ''}] {f['label']}"
                         f" = {f.get('value') or '(empty)'}{opts}")
    lines.append("Buttons: " + (", ".join(f"{b['ref']} \"{b['text']}\"" for b in form["buttons"]) or "none"))
    return "\n".join(lines)


def _summary(form: dict) -> str:
    out = []
    for f in form["fields"]:
        if f["type"] in ("checkbox", "radio"):
            if f.get("checked"):
                out.append(f"{f['label'][:60]}: {f['option']}")
        elif f.get("value") and f.get("value") != "Choose":
            out.append(f"{f['label'][:60]}: {f['value']}")
    return "\n".join(out) or "(nothing filled in)"


async def _read() -> dict:
    page = _page()
    _S["form"] = await page.evaluate(FIELDS_JS)
    return _S["form"]


@function_tool
async def form_open(url: str) -> str:
    """Open a public web form in a cloud browser (nothing opens on the Mac) and list its fields and buttons
    with refs (f0, f1… and b0, b1…). Not for forms that need the user signed in: use the web_* tools for those."""
    from playwright.async_api import async_playwright
    await close()
    try:
        s = await asyncio.to_thread(_api, "POST", BROWSER_URL, {"url": url, "timeout_seconds": IDLE_S})
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(s["cdp_url"], timeout=45000)
    except Exception as e:
        if "s" in locals() and s.get("session_id"):
            _S["id"] = s["session_id"]
            await close()
        return f"Error: {e}"
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    _S.update(id=s["session_id"], pw=pw, browser=browser, page=page, at=time.time(), opened=time.time())
    try:
        if page.url in ("", "about:blank"):
            await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    return _show(await _read())


class FieldValue(BaseModel):
    ref: str
    value: str


@function_tool
async def form_fill(values: list[FieldValue]) -> str:
    """Fill in fields of the open form. values: [{ref, value}]. For a checkbox value "yes" or "no"; for a radio
    button or radio option any value checks it; for a dropdown, the option's text. Only use values the user gave
    you or that you know for sure; ask for anything else. Returns the form as it is now."""
    page, form = _page(), _S.get("form") or await _read()
    types = {f["ref"]: f["type"] for f in form["fields"]}
    errs = []
    for v in values:
        loc, t = page.locator(f'[data-jv="{v.ref}"]'), types.get(v.ref)
        try:
            if t is None:
                errs.append(f"{v.ref}: no such field")
            elif t == "select":
                try:
                    await loc.select_option(label=v.value, timeout=5000)
                except Exception:
                    await loc.select_option(v.value, timeout=5000)
            elif t == "checkbox":
                await (loc.check if v.value.strip().lower() in ("yes", "true", "on", "checked", "1") else loc.uncheck)(timeout=5000)
            elif t == "radio":
                await loc.check(timeout=5000)
            else:
                await loc.fill(v.value, timeout=5000)
        except Exception as e:
            if t in ("checkbox", "radio"):
                try:
                    await loc.click(timeout=5000, force=True)
                    continue
                except Exception:
                    pass
            errs.append(f"{v.ref}: {str(e).splitlines()[0][:120]}")
    return ("Couldn't fill: " + "; ".join(errs) + "\n\n" if errs else "") + _show(await _read())


@function_tool
async def form_click(ref: str) -> str:
    """Click a button on the open form that moves it along without sending it, like "Next" or "Add another".
    Submit / Send / RSVP / Register buttons go through form_submit instead."""
    page, form = _page(), _S.get("form") or await _read()
    b = next((b for b in form["buttons"] if b["ref"] == ref), None)
    if not b:
        return "No such button."
    if SUBMITS.search(b["text"]):
        return f"\"{b['text']}\" sends the form: use form_submit."
    await page.locator(f'[data-jv="{ref}"]').click(timeout=10000)
    await asyncio.sleep(1.5)
    return _show(await _read())


@function_tool
async def form_submit(button: str = "") -> str:
    """Submit the open form. The first call shows the user exactly what's filled in and doesn't submit: then
    end your reply with "Submit it?". Call it again after the user says yes. button: the submit button's ref
    if there are several."""
    page, form = _page(), await _read()
    sub = [b for b in form["buttons"] if SUBMITS.search(b["text"])]
    b = next((x for x in form["buttons"] if x["ref"] == button), None) if button else (sub[0] if len(sub) == 1 else None)
    if not b:
        return "Which button submits it? Pass its ref: " + ", ".join(f"{x['ref']} \"{x['text']}\"" for x in form["buttons"])
    host = urllib.parse.urlsplit(form["url"]).hostname or form["url"]
    summary = _summary(form)
    if why := decisions.gate("form", host, summary, subject=form["title"]):
        return why.replace("Send it?", "Submit it?")
    await page.locator(f'[data-jv="{b["ref"]}"]').click(timeout=10000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1)
    text = (await page.inner_text("body"))[:1500]
    mins = (time.time() - _S.get("opened", time.time())) / 60
    await close()
    result = f"Clicked \"{b['text']}\". The page now says:\n{text}\n\n(Cloud browser closed after {mins:.0f} min.)"
    decisions.sent("form", host, summary, result[:500])
    return result


@function_tool
async def form_close() -> str:
    """Close the cloud browser without submitting."""
    await close()
    return "Closed, nothing submitted."


form_open.timeout_seconds = 120
form_submit.timeout_seconds = 90
FORM_TOOLS = [form_open, form_fill, form_click, form_submit, form_close]
FORM_RULE = """
Public web forms (an RSVP, a sign-up, a contact or booking form that doesn't need the user signed in): use
form_open, form_fill (and form_click for "Next"), then form_submit, all in the cloud so nothing opens on the
Mac. Fill only what the user told you or you know for sure (their name, email and details from People "me");
ask for the rest. form_submit first shows the user the filled-in form: end your reply with one line saying
what you filled in and "Submit it?". Only after their yes, call form_submit again. If they say no, form_close.
Forms that need the user signed in use the web_* tools in Dia instead.
Page watches ("tell me when this drops under $200", "let me know when it's back in stock"): find the page
(search_web if needed) and call watch_page. The checks run in the cloud for free.
"""

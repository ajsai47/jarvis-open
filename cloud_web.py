"""Cloud web tools (TinyFish Search and Fetch): look things up and read public pages without
opening anything on the Mac. Both are free within TinyFish's limits. Key: TINYFISH_API_KEY in .env."""
import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from agents import function_tool

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
NO_KEY = "Cloud web isn't set up: add TINYFISH_API_KEY to mac-copilot/.env. Use the web_* tools instead."
MAX_PAGES = 5
PAGE_CHARS = 12000


def _call(url: str, body: dict | None = None, timeout: int = 45) -> dict:
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        raise RuntimeError(NO_KEY)
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("TinyFish's free limit is used up for now. Use the web_* tools instead.")
        if e.code in (401, 403):
            raise RuntimeError("TinyFish rejected the key (TINYFISH_API_KEY in .env).")
        raise RuntimeError(f"TinyFish error {e.code}.")


@function_tool
async def search_web(query: str, news: bool = False) -> str:
    """Search the web in the cloud (nothing opens on the Mac). Returns the top results with title,
    url and snippet. news=True searches recent news. Use for public facts, prices, products, places."""
    params = {"query": query, "location": "US", "language": "en"}
    if news:
        params["domain_type"] = "news"
    try:
        r = await asyncio.to_thread(_call, f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
    except Exception as e:
        return f"Error: {e}"
    rows = [f"{x.get('position')}. {x.get('title')} — {x.get('url')}\n   {x.get('snippet', '')}"
            for x in (r.get("results") or [])[:8]]
    return "\n".join(rows) or "No results."


@function_tool
async def read_pages(urls: list[str]) -> str:
    """Read public web pages in the cloud (nothing opens on the Mac) and return their text.
    Up to 5 urls at once. Doesn't work for pages that need the user to be signed in."""
    try:
        r = await asyncio.to_thread(_call, FETCH_URL, {"urls": urls[:MAX_PAGES]}, 60)
    except Exception as e:
        return f"Error: {e}"
    out = [f"## {p.get('title') or ''} ({p.get('final_url') or p.get('url')})\n{(p.get('text') or '')[:PAGE_CHARS]}"
           for p in r.get("results") or []]
    out += [f"## Couldn't read {e.get('url')}: {e.get('error')}" for e in r.get("errors") or []]
    return "\n\n".join(out) or "Nothing came back."


CLOUD_TOOLS = [search_web, read_pages]
CLOUD_READ = {"search_web", "read_pages"}
CLOUD_RULE = """
Public web info (prices, facts, products, news, opening hours, any public page): use search_web
and read_pages first. They run in the cloud, so nothing opens on the Mac, and they're faster than
a Dia tab. Use the web_* tools instead when the page needs the user signed in, when they mean a
page they have open, or to click and fill things in. Never put private details (message contents,
email addresses, phone numbers) into search_web.
"""

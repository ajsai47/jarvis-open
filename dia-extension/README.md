# Jarvis for Dia

Lets Jarvis work in your real Dia, in its own background tabs (grouped as "Jarvis"), signed in as you.
It only talks to Jarvis on this Mac (127.0.0.1:8765), and Jarvis only accepts this extension's ID
(see EXTENSION_ID).

Install once:
1. In Dia, open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked**.
4. Pick the folder `~/mac-copilot/dia-extension`.

While Jarvis is working in a tab, Dia shows a bar saying the extension is debugging the browser.
The bar goes away about 2 minutes after Jarvis finishes.

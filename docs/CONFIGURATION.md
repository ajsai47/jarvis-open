# Configuration

Use local environment variables or this checkout's untracked `.env` for integrations you intentionally enable. Never copy a real `.env`, OAuth client or refresh-token file, people/rules file, or `logs/` into a distributable snapshot. The source does not import another app's environment file.

| Integration | Setup |
| --- | --- |
| OpenAI | Set `OPENAI_API_KEY` for OpenAI features. |
| TypeSafe | Optionally set `TYPESAFE_API_KEY`. Enabled routing sends request text to TypeSafe. |
| Slack | Optionally set `SLACK_USER_TOKEN` for an account you authorize; `JARVIS_SLACK_ROSTER` is an optional explicit path to your local contact cache. |
| Google | Add your own desktop OAuth client as `google_client.json`; run `app/Connect Gmail.command` using zsh to start sign-in. Optional `JARVIS_GOOGLE_LOGIN_HINT` is explicit. |
| Local models | Run Ollama locally, install the chosen model, then select it in Settings. If Ollama is unavailable, the configured work path can fall back to OpenAI. |
| Local voice | Create a Python 3.12 `.venv-voice`, install `requirements-voice.txt`, and choose the local voice engine in Settings. |
| Mac tools | Provide `agent-desktop` on PATH if using app control. Grant only the macOS permissions required by the chosen tools, such as Accessibility, Microphone, Screen Recording, or Full Disk Access. |
| Dia | Load the unpacked `dia-extension` only if you want Jarvis to access the browser. |
| Phone | Optional Apple WebDriverAgent integration for a spare developer-mode iPhone; see `phone_tools.py` for required environment options. It is not a native iOS app. |
| TinyFish | Optional public web integration; configure only if wanted and review its data handling. |
| Mission Control | Optional local companion, not part of this snapshot. |
| Claude Mem | A local integration may receive decision records, unless `JARVIS_NO_CLAUDE_MEM` is set. |

`zsh app/install.sh` and `zsh app/install_notch.sh` install background applications on macOS; only run them after reviewing what they do. No setup step is required just to review this source.

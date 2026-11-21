#!/bin/zsh
# Mac Copilot: an OpenAI agent that operates your Mac through agent-desktop.
cd -- "$(dirname -- "$0")" || exit 1
clear
echo "=========================="
echo "      M A C   C O P I L O T"
echo "=========================="
echo "  1) Talk (voice)"
echo "  2) Type (text chat)"
echo "  3) Web page (Jarvis-style, in your browser)"
read "MODE?Pick 1, 2 or 3: "
echo
if [ "$MODE" = "1" ]; then
  .venv/bin/python copilot_voice.py
elif [ "$MODE" = "3" ]; then
  .venv/bin/python copilot_web.py
else
  .venv/bin/python copilot_text.py
fi
echo
read -k1 "?Press any key to close."

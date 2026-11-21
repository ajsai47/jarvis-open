#!/bin/zsh
# One-time: let Mac Copilot read and send Gmail. Opens a Google sign-in page.
cd -- "$(dirname -- "$0")/.." || exit 1
clear
echo "Connect Gmail to Mac Copilot"
echo
.venv/bin/python gmail_auth.py
echo
read -k1 "?Press any key to close."

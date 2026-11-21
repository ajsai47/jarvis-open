#!/bin/zsh
# Builds ~/Applications/Mac Copilot.app and makes it start at login, in the background with no
# Terminal window. Run once: zsh app/install.sh
set -e
REPO="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
APP="$HOME/Applications/Mac Copilot.app"
AGENT="$HOME/Library/LaunchAgents/org.jarvisopen.maccopilot.plist"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents"

rm -rf "$APP"
osacompile -o "$APP" -e "do shell script \"/bin/zsh '$REPO/app/serve.sh' > /dev/null 2>&1 &\""
P="$APP/Contents/Info.plist"
plutil -replace LSUIElement -bool true "$P"
plutil -replace CFBundleIdentifier -string org.jarvisopen.maccopilot "$P"
plutil -replace NSMicrophoneUsageDescription -string "Mac Copilot listens only while you hold Option + Command." "$P"
plutil -replace NSAppleEventsUsageDescription -string "Mac Copilot sends texts through Messages only after you say Yes." "$P"
codesign --force --deep -s - "$APP"

cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.jarvisopen.maccopilot</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-g</string><string>-a</string><string>$APP</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
echo "Installed $APP (starts at login)"

#!/bin/zsh
# Builds ~/Applications/Jarvis Notch.app (Jarvis in the MacBook notch) and keeps it running from login.
# Run: zsh app/install_notch.sh    Stop: zsh app/install_notch.sh --stop
set -e
REPO="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
APP="$HOME/Applications/Jarvis Notch.app"
LABEL=org.jarvisopen.jarvisnotch
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ $1 == --stop ]]; then rm -f "$AGENT"; echo "Jarvis Notch stopped"; exit 0; fi

mkdir -p "$APP/Contents/MacOS" "$HOME/Library/LaunchAgents"
swiftc -O -swift-version 5 -parse-as-library "$REPO/notch/JarvisNotch.swift" -o "$APP/Contents/MacOS/JarvisNotch"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>JarvisNotch</string>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>Jarvis Notch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force -s - "$APP"

cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/JarvisNotch</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$AGENT"
echo "Jarvis Notch installed and running"

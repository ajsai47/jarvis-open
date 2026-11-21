#!/bin/zsh
# Runs the Mac Copilot web server in the background and restarts it if it stops.
# Started by Mac Copilot.app (at login, or when double-clicked). `serve.sh --stop` stops it.
cd -- "$(dirname -- "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
URL=http://127.0.0.1:8765
PIDF=logs/serve.pid
mkdir -p logs

up() { curl -s -m 2 -H "X-Mac-Copilot: 1" $URL/api/status >/dev/null; }
# After a reboot the old PID can belong to another process, so check it is still serve.sh.
running() { [[ -f $PIDF ]] && ps -o command= -p "$(<$PIDF)" 2>/dev/null | grep -q serve.sh; }
just_logged_in() {
  local t=$(ps -o etime= -p "$(pgrep -x loginwindow | head -1)" | tr -d ' ')
  [[ $t != *-* && $t != *:*:* ]] && (( ${t%%:*} < 3 ))
}

if [[ $1 == --stop ]]; then
  running && kill "$(<$PIDF)"
  rm -f $PIDF
  pkill -f "copilot_web.py" && echo stopped
  exit 0
fi

if running || up; then
  just_logged_in || open $URL
  exit 0
fi

echo $$ > $PIDF
trap 'pkill -P $$; rm -f $PIDF' EXIT
if ! just_logged_in; then
  ( for _ in {1..30}; do sleep 1; up && { open $URL; break; }; done ) &
fi
while true; do
  if up; then sleep 10; continue; fi
  echo "--- start $(date)" >> logs/server.log
  .venv/bin/python copilot_web.py --no-browser >> logs/server.log 2>&1
  sleep 3
done

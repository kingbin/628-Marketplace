#!/usr/bin/env bash
# CMC — open/reopen the dashboard panel via ~/Applications/CMC.app.
#
# CMC.app is launched through LaunchServices (`open -n -a`) so it gets its
# own dock icon, Mission Control space, and AppleScript identity. The regular
# /Applications/Google Chrome.app is never touched — its profile picker and
# windows are unaffected.

set -o pipefail

CONFIG_FILE="${HOME}/.claude/cmc.json"
SERVER_PID_FILE="/tmp/cmc-server.pid"
SERVER_PORT=7477
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
CMC_APP="${HOME}/Applications/CMC.app"
CMC_BUNDLE_ID="com.628productions.cmc"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="${SCRIPT_DIR}/cmc-server.py"

[ -d "$CMC_APP" ] || { echo "CMC.app not found at $CMC_APP"; exit 1; }

# ── Read config ───────────────────────────────────────────────────────────────
PANEL_W=$(python3 -c "
import json
try:
    with open('${CONFIG_FILE}') as f:
        print(json.load(f).get('panel_width', 350))
except Exception:
    print(350)
" 2>/dev/null)

# ── Start server if not already running ──────────────────────────────────────
if [ -f "$SERVER_PID_FILE" ]; then
    saved_pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || echo "")
    if [ -z "$saved_pid" ] || ! kill -0 "$saved_pid" 2>/dev/null; then
        rm -f "$SERVER_PID_FILE"
    fi
fi

if [ ! -f "$SERVER_PID_FILE" ]; then
    [ -f "$SERVER_SCRIPT" ] || { echo "cmc-server.py not found"; exit 1; }
    nohup python3 "$SERVER_SCRIPT" > /tmp/cmc-server.log 2>&1 &
    echo $! > "$SERVER_PID_FILE"
    for i in $(seq 1 10); do
        curl -sf "${SERVER_URL}/" > /dev/null 2>&1 && break
        sleep 0.5
    done
fi

# ── Open or focus the CMC.app panel ──────────────────────────────────────────
python3 - "${SERVER_URL}" "${PANEL_W}" "${CMC_APP}" "${CMC_BUNDLE_ID}" << 'PYEOF'
import subprocess, sys, time

url       = sys.argv[1]
panel_w   = int(sys.argv[2])
cmc_app   = sys.argv[3]
bundle_id = sys.argv[4]

r = subprocess.run(
    ["osascript", "-e",
     'tell application "Finder" to return '
     '(item 3 of bounds of window of desktop) as string '
     '& "," & (item 4 of bounds of window of desktop) as string'],
    capture_output=True, text=True, timeout=5,
)
parts = r.stdout.strip().split(",")
sw = int(parts[0]) if len(parts) == 2 else 1920
sh = int(parts[1]) if len(parts) == 2 else 1080
panel_x = sw - panel_w

# A running CMC is identified by its private --user-data-dir. This match is
# unique to CMC and never matches a regular Google Chrome.app process.
already_running = subprocess.run(
    ["pgrep", "-f", "cmc-chrome-profile"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
).returncode == 0

if already_running:
    # Bring CMC to the front. Targeting the bundle id never resolves to the
    # regular Google Chrome app, so other Chrome windows stay put.
    subprocess.run(
        ["osascript", "-e",
         f'tell application id "{bundle_id}" to activate'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
else:
    # LaunchServices launch → CMC gets its own dock entry and space.
    # --user-data-dir isolates CMC from the user's regular Chrome profile so
    # that launching Chrome while CMC is running still shows the profile picker.
    subprocess.Popen(
        ["open", "-n", "-a", cmc_app, "--args",
         "--user-data-dir=/tmp/cmc-chrome-profile",
         "--no-first-run",
         "--no-default-browser-check",
         f"--window-position={panel_x},0",
         f"--window-size={panel_w},{sh}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

time.sleep(2.0)
subprocess.run(
    ["osascript", "-e",
     f'tell application id "{bundle_id}" to try\n'
     f'  set bounds of front window to {{{panel_x}, 0, {sw}, {sh}}}\n'
     f'end try'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(f"CMC opened at {url}")
PYEOF

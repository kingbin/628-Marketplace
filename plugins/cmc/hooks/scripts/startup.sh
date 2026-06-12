#!/usr/bin/env bash
# CMC — SessionStart hook
# Reads ~/.claude/cmc.json for configuration.
#
# Config options:
#   browser     "chrome"   — Chrome --app mode, frameless standalone window (default)
#               "default"  — system default browser (new standalone window)
#   panel_width  350       — panel width in pixels

set -o pipefail

SERVER_SCRIPT="${CLAUDE_PLUGIN_ROOT}/hooks/scripts/cmc-server.py"
SERVER_PID_FILE="/tmp/cmc-server.pid"
SERVER_PORT=7477
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
CONFIG_FILE="${HOME}/.claude/cmc.json"

[ -f "$SERVER_SCRIPT" ] || exit 0

# ── Check enabled flag ────────────────────────────────────────────────────────
# If config exists and enabled=false, shut down any running server and exit.
ENABLED=$(python3 -c "
import json, sys
try:
    with open('${CONFIG_FILE}') as f:
        print(json.load(f).get('enabled', True))
except Exception:
    print(True)
" 2>/dev/null)

if [ "$ENABLED" = "False" ]; then
    # Kill server if running
    if [ -f "$SERVER_PID_FILE" ]; then
        saved_pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || echo "")
        [ -n "$saved_pid" ] && kill "$saved_pid" 2>/dev/null || true
        rm -f "$SERVER_PID_FILE"
    fi
    # Close Chrome panel
    osascript 2>/dev/null << 'CLOSE_EOF' || true
tell application "Google Chrome"
    repeat with w in windows
        try
            if URL of active tab of w starts with "http://127.0.0.1:7477" then close w
        end try
    end repeat
end tell
CLOSE_EOF
    exit 0
fi

# ── First-run: seed config from the template (silent — /cmc-setup is the
# interactive path; a SessionStart hook must never block on dialogs) ─────────
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_TEMPLATE="${CLAUDE_PLUGIN_ROOT}/hooks/scripts/cmc-config.template.json"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    [ -f "$CONFIG_TEMPLATE" ] && cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
fi

# ── Start server if not already running ──────────────────────────────────────
if [ -f "$SERVER_PID_FILE" ]; then
    saved_pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || echo "")
    if [ -n "$saved_pid" ] && kill -0 "$saved_pid" 2>/dev/null; then
        exit 0  # server + panel already running
    fi
    rm -f "$SERVER_PID_FILE"
fi

nohup python3 "$SERVER_SCRIPT" > /tmp/cmc-server.log 2>&1 &
echo $! > "$SERVER_PID_FILE"

# Wait up to 5s for server to be ready
for i in $(seq 1 10); do
    if curl -sf "${SERVER_URL}/" > /dev/null 2>&1; then break; fi
    sleep 0.5
done

# ── Read config + open panel ──────────────────────────────────────────────────
python3 - "${SERVER_URL}" "${CONFIG_FILE}" << 'PYEOF'
import json, os, pathlib, plistlib, subprocess, sys, time

url         = sys.argv[1]
config_path = sys.argv[2]

# Load config (fall back to defaults if missing/malformed)
config = {}
try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    pass

browser  = config.get("browser", "chrome")   # "chrome" | "default"
panel_w  = int(config.get("panel_width", 350))

# ── Screen dimensions ─────────────────────────────────────────────────────────
def screen_size():
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "Finder" to return '
         '(item 3 of bounds of window of desktop) as string '
         '& "," & (item 4 of bounds of window of desktop) as string'],
        capture_output=True, text=True, timeout=5,
    )
    parts = r.stdout.strip().split(",")
    try:
        return int(parts[0]), int(parts[1])
    except Exception:
        return 1920, 1080

sw, sh = screen_size()
panel_x = sw - panel_w

# ── CMC.app launcher (own dock icon, own Mission Control space) ──────────────
# CMC.app wraps Chrome with a dedicated --user-data-dir, so launching it via
# LaunchServices yields a separate app identity that never collides with the
# user's regular /Applications/Google Chrome.app windows or profiles.
CMC_APP = os.path.expanduser("~/Applications/CMC.app")
CMC_BUNDLE_ID = "com.628productions.cmc"

CHROMIUM_BINS = {
    "com.google.chrome":          ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",        "Google Chrome"),
    "com.google.chrome.canary":   ("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary", "Google Chrome Canary"),
    "com.brave.browser":          ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",        "Brave Browser"),
    "com.microsoft.edgemac":      ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",      "Microsoft Edge"),
    "company.thebrowser.browser": ("/Applications/Arc.app/Contents/MacOS/Arc",                            "Arc"),
}

def launch_cmc_app():
    subprocess.Popen(
        ["open", "-n", "-a", CMC_APP, "--args",
         "--user-data-dir=/tmp/cmc-chrome-profile",
         "--no-first-run",
         "--no-default-browser-check",
         f"--window-position={panel_x},0",
         f"--window-size={panel_w},{sh}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    # Position via the CMC bundle id; never addresses "Google Chrome",
    # so regular Chrome windows are never resized.
    subprocess.run(
        ["osascript", "-e",
         f'tell application id "{CMC_BUNDLE_ID}" to try\n'
         f'  set bounds of front window to {{{panel_x}, 0, {sw}, {sh}}}\n'
         f'end try'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def launch_chromium(binary, app_name):
    subprocess.Popen(
        [binary,
         f"--app={url}",
         f"--window-position={panel_x},0",
         f"--window-size={panel_w},{sh}",
         "--user-data-dir=/tmp/cmc-chrome-profile",
         "--no-first-run",
         "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    # AppleScript fallback for positioning (CLI flags not always honoured on macOS)
    subprocess.run(
        ["osascript", "-e",
         f'tell application "{app_name}" to try\n'
         f'  set bounds of front window to {{{panel_x}, 0, {sw}, {sh}}}\n'
         f'end try'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

# ── "chrome" mode: launch CMC.app so it lives in its own space ───────────────
if browser == "chrome":
    if not os.path.exists(CMC_APP):
        # Not built yet — /cmc-setup creates it. Don't fail the SessionStart hook.
        print(f"CMC.app not found at {CMC_APP}; run /cmc-setup to build it.")
        sys.exit(0)
    launch_cmc_app()
    sys.exit(0)

# ── "default" mode: use the system default browser ───────────────────────────
bundle_id = ""
try:
    p = (pathlib.Path.home()
         / "Library/Preferences/com.apple.LaunchServices"
         / "com.apple.launchservices.secure.plist")
    with open(p, "rb") as f:
        data = plistlib.load(f)
    for h in data.get("LSHandlers", []):
        if h.get("LSHandlerURLScheme") == "https":
            bundle_id = h.get("LSHandlerRoleAll", "").lower()
            break
except Exception:
    pass

# Chromium-based default browser → app mode
if bundle_id in CHROMIUM_BINS:
    binary, app_name = CHROMIUM_BINS[bundle_id]
    if os.path.exists(binary):
        launch_chromium(binary, app_name)
        sys.exit(0)

# Safari → new document (new window, not a tab)
if bundle_id == "com.apple.safari":
    subprocess.run(
        ["osascript", "-e",
         f'tell application "Safari"\n'
         f'  make new document with properties {{URL:"{url}"}}\n'
         f'  activate\n'
         f'  try\n'
         f'    set bounds of front window to {{{panel_x}, 0, {sw}, {sh}}}\n'
         f'  end try\n'
         f'end tell'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sys.exit(0)

# Firefox → --new-window flag
if bundle_id == "org.mozilla.firefox":
    ff = "/Applications/Firefox.app/Contents/MacOS/firefox"
    if os.path.exists(ff):
        subprocess.Popen([ff, "--new-window", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)
        subprocess.run(
            ["osascript", "-e",
             f'tell application "Firefox" to try\n'
             f'  set bounds of front window to {{{panel_x}, 0, {sw}, {sh}}}\n'
             f'end try'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sys.exit(0)

# Unknown default browser → open -n (new instance)
subprocess.Popen(["open", "-n", url],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PYEOF

#!/usr/bin/env bash
#
# cmc-setup.sh — one-time setup for the CMC plugin.
#
# Idempotent. Safe to re-run. Performs:
#   1. Dependency check (python3, Google Chrome, curl, lsof, pgrep, osascript)
#   2. Build ~/Applications/CMC.app (Chrome --app wrapper with its own identity)
#   3. Seed ~/.claude/cmc.json from the template if it does not exist
#
set -o pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
say()  { echo -e "${BLUE}==>${NC} $*"; }
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# plugin root = .../plugins/cmc  (scripts → hooks → cmc)
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ICON_SRC="${PLUGIN_ROOT}/assets/claude.icns"

CMC_APP="${HOME}/Applications/CMC.app"
CMC_BUNDLE_ID="com.628productions.cmc"
CHROME_APP="/Applications/Google Chrome.app"
CHROME_BIN="${CHROME_APP}/Contents/MacOS/Google Chrome"
SERVER_PORT=7477
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"

CONFIG_FILE="${HOME}/.claude/cmc.json"
CONFIG_TEMPLATE="${SCRIPT_DIR}/cmc-config.template.json"

# ── 1. Dependency check ───────────────────────────────────────────────────────
say "Checking dependencies"
missing=0

check_cmd() {  # check_cmd <name> <required:yes|no>
    if command -v "$1" >/dev/null 2>&1; then
        ok "$1 ($(command -v "$1"))"
    else
        if [[ "$2" == "yes" ]]; then bad "$1 — required, not found"; missing=$((missing+1));
        else warn "$1 — optional, not found"; fi
    fi
}

check_cmd python3 yes
check_cmd curl    yes
check_cmd lsof    yes
check_cmd pgrep   yes
check_cmd osascript yes   # macOS built-in; absent off-macOS

if [[ -d "$CHROME_APP" ]]; then
    ok "Google Chrome ($CHROME_APP)"
else
    bad "Google Chrome not found at $CHROME_APP — required to render the panel"
    missing=$((missing+1))
fi

# The server is pure Python stdlib — no pip packages needed.
if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PYEOF' 2>/dev/null
import http.server, urllib.parse, json, secrets, glob, subprocess  # noqa
PYEOF
    then ok "python3 stdlib modules present (no pip install needed)"
    else bad "python3 cannot import required stdlib modules"; missing=$((missing+1)); fi
fi

if (( missing > 0 )); then
    echo
    bad "${missing} required dependency/dependencies missing — resolve the above, then re-run."
    exit 1
fi

# ── 2. Build ~/Applications/CMC.app ───────────────────────────────────────────
echo
say "Building ${CMC_APP}"

[[ -f "$ICON_SRC" ]] || { warn "icon not found at $ICON_SRC (app will use a generic icon)"; }

# Rebuild cleanly so re-runs always reflect the current template.
rm -rf "$CMC_APP"
mkdir -p "${CMC_APP}/Contents/MacOS" "${CMC_APP}/Contents/Resources"

cat > "${CMC_APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>CMC</string>
    <key>CFBundleIconFile</key>
    <string>claude</string>
    <key>CFBundleIdentifier</key>
    <string>${CMC_BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>CMC</string>
    <key>CFBundleDisplayName</key>
    <string>CMC</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "${CMC_APP}/Contents/MacOS/CMC" <<LAUNCHER
#!/bin/bash
exec "${CHROME_BIN}" \\
    --app="${SERVER_URL}" \\
    --user-data-dir="/tmp/cmc-chrome-profile" \\
    --no-first-run \\
    --no-default-browser-check \\
    "\$@"
LAUNCHER
chmod +x "${CMC_APP}/Contents/MacOS/CMC"

if [[ -f "$ICON_SRC" ]]; then
    cp "$ICON_SRC" "${CMC_APP}/Contents/Resources/claude.icns"
fi

# Nudge LaunchServices to register the (re)built bundle.
touch "$CMC_APP"
ok "CMC.app built (bundle id ${CMC_BUNDLE_ID})"

# ── 3. Seed ~/.claude/cmc.json ────────────────────────────────────────────────
echo
say "Config"
mkdir -p "$(dirname "$CONFIG_FILE")"
if [[ -f "$CONFIG_FILE" ]]; then
    ok "config already exists at $CONFIG_FILE (left untouched)"
elif [[ -f "$CONFIG_TEMPLATE" ]]; then
    cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
    ok "seeded $CONFIG_FILE from template"
else
    warn "no template at $CONFIG_TEMPLATE; skipping config seed (defaults apply)"
fi

echo
echo -e "${GREEN}CMC setup complete.${NC} Run /cmc to open the dashboard panel."

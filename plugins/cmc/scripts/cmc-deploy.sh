#!/usr/bin/env bash
#
# cmc-deploy.sh — push the CMC source into the live install and restart the server.
#
# Fully path-agnostic / portable: resolves the marketplace source from Claude
# Code's known_marketplaces.json (so it works wherever 628-Marketplace is cloned),
# reads the version from plugin.json, and derives the live cache under $HOME. No
# hardcoded user or version. Carry it between machines unchanged.
#
# Usage:
#   cmc-deploy.sh            # sync source → cache, restart, verify
#   cmc-deploy.sh --dry-run  # preview the rsync changes only
#
set -euo pipefail

MARKETPLACE="628-Marketplace"
KNOWN_MP="$HOME/.claude/plugins/known_marketplaces.json"
CACHE_PARENT="$HOME/.claude/plugins/cache/${MARKETPLACE}/cmc"

PIDFILE="/tmp/cmc-server.pid"
LOGFILE="/tmp/cmc-server.log"
PORT=7477
URL="http://127.0.0.1:${PORT}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
say()  { echo -e "${BLUE}==>${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
die()  { echo -e "${RED}error:${NC} $*" >&2; exit 1; }

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

RSYNC_EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store')

# ── Resolve the editable source from the marketplace registration ─────────────
# Prefer a local directory source (dev machine); fall back to the install
# location (cloned GitHub marketplace). Empty if neither is usable.
MP_DIR=""
if [[ -f "$KNOWN_MP" ]]; then
    MP_DIR=$(python3 - "$KNOWN_MP" "$MARKETPLACE" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1])).get(sys.argv[2], {})
    print((d.get("source") or {}).get("path") or d.get("installLocation") or "")
except Exception:
    print("")
PY
)
fi
SRC="${MP_DIR:+$MP_DIR/plugins/cmc}"

# ── Decide sync-from-source vs restart-cache-only ─────────────────────────────
if [[ -n "$SRC" && -f "$SRC/.claude-plugin/plugin.json" ]]; then
    VERSION=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" \
        "$SRC/.claude-plugin/plugin.json") || die "could not read version from $SRC"
    CACHE="$CACHE_PARENT/$VERSION"
    DO_SYNC=true
else
    # No editable source resolved (e.g. GitHub-only install) — just restart the
    # latest cached copy in place.
    CACHE=$(ls -d "$CACHE_PARENT"/*/ 2>/dev/null | sort -V | tail -1)
    CACHE="${CACHE%/}"
    [[ -n "$CACHE" ]] || die "cmc not installed (no source and no cache under $CACHE_PARENT)"
    DO_SYNC=false
    warn "no editable source resolved; will restart the cache copy only"
fi
SERVER="$CACHE/hooks/scripts/cmc-server.py"

say "CMC deploy — marketplace ${MARKETPLACE}"
[[ -n "$SRC" ]] && echo "    source: $SRC"
echo "    cache:  $CACHE"
echo

# ── Dry run ───────────────────────────────────────────────────────────────────
if $DRY_RUN; then
    if $DO_SYNC; then
        say "DRY RUN — changes that would be synced to cache:"
        rsync -ai --delete --dry-run "${RSYNC_EXCLUDES[@]}" "$SRC/" "$CACHE/" || true
    else
        say "DRY RUN — no editable source; would restart cache copy only."
    fi
    exit 0
fi

# ── 1. Sync source → cache (live) ─────────────────────────────────────────────
if $DO_SYNC; then
    say "Syncing source → cache"
    mkdir -p "$CACHE"
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC/" "$CACHE/"
    ok "cache updated"
fi

# ── 2. Force-restart the server ───────────────────────────────────────────────
say "Restarting CMC server"
if PORT_PIDS=$(lsof -ti "tcp:${PORT}" 2>/dev/null); then
    for p in $PORT_PIDS; do kill "$p" 2>/dev/null || true; done
fi
if STRAY=$(pgrep -f "cmc-server.py" 2>/dev/null); then
    for p in $STRAY; do kill "$p" 2>/dev/null || true; done
fi
sleep 1
if PORT_PIDS=$(lsof -ti "tcp:${PORT}" 2>/dev/null); then
    for p in $PORT_PIDS; do kill -9 "$p" 2>/dev/null || true; done
    sleep 1
fi
rm -f "$PIDFILE"
if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    die "port ${PORT} still held after kill attempts; aborting"
fi
ok "port ${PORT} free"

[[ -f "$SERVER" ]] || die "server script missing: $SERVER"
nohup python3 "$SERVER" > "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
ok "started server (pid $(cat "$PIDFILE"))"

for _ in $(seq 1 20); do
    if curl -sf "${URL}/" >/dev/null 2>&1; then break; fi
    sleep 0.5
done
if ! curl -sf "${URL}/" >/dev/null 2>&1; then
    warn "server did not respond on ${URL}; last log lines:"
    tail -n 15 "$LOGFILE" || true
    die "deploy failed — server not healthy"
fi
ok "server healthy on ${URL}"

# ── 3. Cleanup ────────────────────────────────────────────────────────────────
say "Cleanup"
find "$CACHE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
ok "pruned __pycache__"

# ── 4. Verify ─────────────────────────────────────────────────────────────────
say "Verify"
RUNNING_FILE=$(ps -p "$(cat "$PIDFILE")" -o command= 2>/dev/null | grep -o '[^ ]*cmc-server.py' || true)
if [[ "$RUNNING_FILE" == "$SERVER" ]]; then
    ok "live server is the cache copy"
else
    warn "live server path is '$RUNNING_FILE' (expected $SERVER)"
fi
if curl -sf "${URL}/api/sessions" 2>/dev/null | grep -q '"focused"'; then
    ok "/api/sessions reports the 'focused' field"
else
    warn "/api/sessions has no 'focused' field — running code may be stale"
fi

echo
echo -e "${GREEN}CMC deploy complete.${NC} (Chrome panel will reconnect on its next poll.)"

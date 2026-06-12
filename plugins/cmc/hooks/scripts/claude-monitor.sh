#!/usr/bin/env bash
# Claude Overlord — real-time tile monitor for all Claude Code instances in iTerm2.
#
# Uses ~/.claude/sessions/*.json as the authoritative source for active sessions,
# tmux pane detection for status and focus, and it2 CLI for iTerm2 integration.
#
# Controls:
#   [1-9]  Focus that Claude instance in iTerm2 / tmux pane
#   [r]    Force refresh
#   [q]    Quit

# pipefail only — no set -u: bash 3.2 (macOS default) treats empty array
# expansion ${arr[@]} as an unbound variable under set -u, crashing the loop.
set -o pipefail

# ── ANSI palette ─────────────────────────────────────────────────────────────
R=$'\033[0m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
GREEN=$'\033[32m'
BLUE=$'\033[34m'
CYAN=$'\033[36m'
RED=$'\033[91m'
YELLOW=$'\033[33m'

# ── Runtime state ─────────────────────────────────────────────────────────────
STATE_DIR="/tmp/claude-overlord-state"
ROWS_FILE="/tmp/claude-overlord-rows"
PID_FILE="/tmp/claude-overlord.pid"

mkdir -p "$STATE_DIR"
echo $$ > "$PID_FILE"

cleanup() {
    rm -f "$PID_FILE" "$ROWS_FILE"
    rm -rf "$STATE_DIR"
    tput cnorm 2>/dev/null
    printf "%s" "$R"
    clear
}
trap cleanup EXIT INT TERM

tput civis 2>/dev/null
tput clear 2>/dev/null

printf '\033]0;Claude Overlord\007'
printf '\033]1;🤖 Overlord\007'

[ -n "${ITERM_SESSION_ID:-}" ] && \
    it2 session set-badge "${ITERM_SESSION_ID}" "🤖 Overlord" >/dev/null 2>&1 || true

# ── Session discovery ─────────────────────────────────────────────────────────
#
# Outputs one tab-separated row per active Claude Code instance:
#   SID  PROC  CWD  PID  ACTION  PANE_TARGET
#
# Primary source: ~/.claude/sessions/<PID>.json
#   Claude writes these on startup; presence + live PID = active session.
#
# Status detection (ACTION field):
#   - tmux pane   → tmux capture-pane content analysis
#   - iTerm2 only → it2 session suggest-action
#
# PANE_TARGET (e.g. "DEV:1.1"): set for tmux sessions, empty otherwise.
find_claude_sessions() {
    python3 << 'PYEOF'
import json, os, glob, subprocess

def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1

def is_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

# ── 1. Read Claude session files ─────────────────────────────────────────────
sessions_dir = os.path.expanduser("~/.claude/sessions")
claude_sessions = []
for path in glob.glob(os.path.join(sessions_dir, "*.json")):
    try:
        with open(path) as f:
            data = json.load(f)
        pid = str(data.get("pid", ""))
        if pid and is_alive(pid):
            claude_sessions.append(data)
    except Exception:
        continue

# Sort by startedAt for stable tile ordering
claude_sessions.sort(key=lambda s: s.get("startedAt", 0))

if not claude_sessions:
    raise SystemExit(0)

# ── 2. Build pid → ppid map ───────────────────────────────────────────────────
ppid_of = {}
ps_out, _ = run(["ps", "-eo", "pid,ppid"])
for line in ps_out.splitlines()[1:]:
    parts = line.split()
    if len(parts) == 2:
        ppid_of[parts[0].strip()] = parts[1].strip()

# ── 3. tmux pane map: shell_pid → (pane_target, pane_cwd) ────────────────────
tmux_pane_by_pid = {}
tmux_out, rc = run(["tmux", "list-panes", "-a",
                    "-F", "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}"])
if rc == 0 and tmux_out:
    for line in tmux_out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            tmux_pane_by_pid[parts[0].strip()] = (parts[1], parts[2])

# ── 4. iTerm2 session map: tty → session ─────────────────────────────────────
it2_sessions = []
sessions_json, _ = run(["it2", "session", "list", "--format", "json"])
try:
    it2_sessions = json.loads(sessions_json) if sessions_json else []
except Exception:
    pass

tty_to_it2 = {}
for s in it2_sessions:
    shell_pid = str(s.get("ShellPID", ""))
    if shell_pid:
        tty_out, _ = run(["ps", "-p", shell_pid, "-o", "tty="])
        if tty_out and tty_out != "??":
            tty_to_it2[tty_out] = s

# ── 5. Status helpers ─────────────────────────────────────────────────────────
def suggest_action_tmux(pane_target):
    """Infer Claude state from the visible content of a tmux pane."""
    out, rc = run(["tmux", "capture-pane", "-p", "-t", pane_target], timeout=3)
    if rc != 0 or not out:
        return "none"
    lines = [l for l in out.splitlines() if l.strip()]
    recent = "\n".join(lines[-20:]) if lines else ""
    low = recent.lower()

    # Braille spinners → actively generating
    if any(c in recent for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
        return "wait"
    if "esc to interrupt" in low:
        return "wait"

    # Tool approval prompts
    if any(p in low for p in ("do you want to", "allow tool", "allow this",
                               "run this?", "[y/n]", "(y/n)", "proceed?")):
        return "modal:approve"

    # Has todos / continue prompt
    if "what would you like" in low or ("[ ]" in recent and "todo" in low):
        return "continue"

    # Error state
    if any(p in low for p in ("error:", "failed:", "traceback", "exception:")):
        return "review:error"

    return "none"

def suggest_action_it2(sid):
    out, _ = run(["it2", "session", "suggest-action", sid])
    return out.strip() or "none"

# ── 6. Emit rows ──────────────────────────────────────────────────────────────
for sess in claude_sessions:
    pid  = str(sess["pid"])
    cwd  = sess.get("cwd", "unknown")
    ppid = ppid_of.get(pid, "")

    pane_target = ""
    matched_sid = "unknown"
    action      = "none"
    proc        = "claude"

    if ppid and ppid in tmux_pane_by_pid:
        # Running inside a tmux pane
        pane_target, _ = tmux_pane_by_pid[ppid]
        proc   = f"claude({pane_target})"
        action = suggest_action_tmux(pane_target)
        # Correlate to enclosing iTerm2 session via shell TTY
        tty_out, _ = run(["ps", "-p", ppid, "-o", "tty="])
        if tty_out and tty_out != "??":
            it2 = tty_to_it2.get(tty_out)
            matched_sid = it2.get("SessionID", "tmux-pane") if it2 else "tmux-pane"
        else:
            matched_sid = "tmux-pane"
    else:
        # Running directly in an iTerm2 session (or unknown terminal)
        tty_out, _ = run(["ps", "-p", pid, "-o", "tty="])
        if tty_out and tty_out != "??":
            it2 = tty_to_it2.get(tty_out)
            if it2:
                matched_sid = it2.get("SessionID", "unknown")
                action = suggest_action_it2(matched_sid)

    print(f"{matched_sid}\t{proc}\t{cwd}\t{pid}\t{action}\t{pane_target}")
PYEOF
}

# ── Status classification ─────────────────────────────────────────────────────
# Prints: STATUS_LABEL  COLOR_VAR_NAME  SEVERITY(0-3)
classify_action() {
    local action="$1"
    case "$action" in
        "wait")            echo "Working"        "GREEN"  0 ;;
        "continue")        echo "Has Todos"      "CYAN"   1 ;;
        "tab")             echo "Pending Edits"  "YELLOW" 1 ;;
        "modal:approve")   echo "Needs Approval" "YELLOW" 2 ;;
        "modal:review")    echo "Review Needed"  "RED"    3 ;;
        "review:error")    echo "Error State"    "RED"    3 ;;
        *)                 echo "Idle"           "BLUE"   0 ;;
    esac
}

notify_attention() {
    local sid="$1" label="$2" dir="$3"
    local notif_f="$STATE_DIR/${sid:0:8}.notified"
    [ -f "$notif_f" ] && return
    touch "$notif_f"
    osascript -e "display notification \"${label} in $(basename "$dir")\" with title \"Claude Overlord\" sound name \"Glass\"" 2>/dev/null &
    it2 session set-badge "$sid" "⚠ ${label}" >/dev/null 2>&1 &
}

clear_attention() {
    local sid="$1"
    rm -f "$STATE_DIR/${sid:0:8}.notified"
    it2 session set-badge "$sid" $'\u200b' >/dev/null 2>&1 &
}

# Focus an iTerm2 session or tmux pane by index key press.
focus_session() {
    local sid="$1"
    local pane_target="${2:-}"

    if [ -n "$pane_target" ]; then
        tmux select-pane -t "$pane_target" 2>/dev/null || true
        local window_target="${pane_target%.*}"
        tmux select-window -t "$window_target" 2>/dev/null || true
        osascript -e 'tell application "iTerm2" to activate' 2>/dev/null &
    else
        osascript 2>/dev/null << EOF &
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique ID of s) is "$sid" then
                    select w
                    tell t to select
                    select s
                    activate
                    return
                end if
            end repeat
        end repeat
    end repeat
end tell
EOF
    fi
}

# ── Tile drawing ──────────────────────────────────────────────────────────────
draw_tile() {
    local idx="$1" sid="$2" proc="$3" cwd="$4" action="$5"
    local W=50

    local short_sid="${sid:0:8}"
    local dir="${cwd/#$HOME/~}"
    [ ${#dir} -gt $((W-4)) ] && dir="…${dir: -$((W-5))}"

    read -r label color_name severity <<< "$(classify_action "$action")"
    local bc="${!color_name:-$BLUE}"

    local hr
    hr=$(printf '%*s' "$W" '' | tr ' ' '─')

    local c1 c2 c3 c4
    c1=$(printf " [%d] Claude  Session: %-${W}s" "$idx" "$short_sid")
    c1="${c1:0:$W}"
    c2=$(printf "  %-$((W-2))s" "$dir")
    c3=$(printf "  Process: %-$((W-12))s" "$proc")
    c4=$(printf "  Status:  %-$((W-11))s" "$label  [action: ${action:-none}]")
    c2="${c2:0:$W}"; c3="${c3:0:$W}"; c4="${c4:0:$W}"

    printf "${bc}${BOLD}┌${hr}┐${R}\n"
    printf "${bc}│${R}${BOLD}%-${W}s${bc}│${R}\n"       "$c1"
    printf "${bc}│${R}${DIM}%-${W}s${bc}│${R}\n"        "$c2"
    printf "${bc}│${R}%-${W}s${bc}│${R}\n"              "$c3"
    printf "${bc}│${R}${bc}${BOLD}%-${W}s${bc}│${R}\n"  "$c4"
    printf "${bc}${BOLD}└${hr}┘${R}\n"
}

# ── Main render ───────────────────────────────────────────────────────────────
render() {
    local session_rows=()
    local sids=()
    local row
    while IFS= read -r row; do
        [ -n "$row" ] || continue
        session_rows+=("$row")
        sids+=("$(printf '%s' "$row" | cut -f1)")
    done < <(find_claude_sessions)

    # Persist rows for key navigation
    if [ ${#session_rows[@]} -gt 0 ]; then
        printf '%s\n' "${session_rows[@]}" > "$ROWS_FILE" 2>/dev/null || true
    fi

    # ── Attention scan ────────────────────────────────────────────────────────
    local attention=0
    for i in ${!session_rows[@]+"${!session_rows[@]}"}; do
        local sid proc cwd pid action pane_target
        IFS=$'\t' read -r sid proc cwd pid action pane_target <<< "${session_rows[$i]}"
        action="${action:-none}"
        case "$action" in
            modal:*|review:*)
                ((attention++)) || true
                local label
                read -r label _ _ <<< "$(classify_action "$action")"
                notify_attention "$sid" "$label" "$cwd"
                ;;
            *)
                clear_attention "$sid"
                ;;
        esac
    done

    # ── Badge ────────────────────────────────────────────────────────────────
    if [ "${ITERM_SESSION_ID:-}" ]; then
        if [ "$attention" -gt 0 ]; then
            it2 session set-badge "${ITERM_SESSION_ID}" "⚠ ${attention}" >/dev/null 2>&1 || true
        else
            it2 session set-badge "${ITERM_SESSION_ID}" "🤖 Overlord" >/dev/null 2>&1 || true
        fi
    fi

    # ── Header ───────────────────────────────────────────────────────────────
    tput cup 0 0
    printf "${BOLD}${CYAN}╔═══════════════════════════════════════════════════════════╗\n"
    printf "║  🤖  CLAUDE OVERLORD                       %s  ║\n" "$(date '+%H:%M:%S')"
    printf "║  Instances: %-3d  │  Needs Attention: %-3d                     ║\n" \
        "${#sids[@]}" "$attention"
    printf "╚═══════════════════════════════════════════════════════════╝${R}\n\n"

    if [ "${#sids[@]}" -eq 0 ]; then
        printf "${DIM}  No Claude Code instances detected.\n"
        printf "  Watching for new sessions every 3s...${R}\n"
        tput ed 2>/dev/null
        return
    fi

    # ── Tiles ────────────────────────────────────────────────────────────────
    for i in ${!session_rows[@]+"${!session_rows[@]}"}; do
        local sid proc cwd pid action pane_target
        IFS=$'\t' read -r sid proc cwd pid action pane_target <<< "${session_rows[$i]}"
        draw_tile "$((i+1))" "$sid" "$proc" "$cwd" "${action:-none}"
        printf "\n"
    done

    tput ed 2>/dev/null
    printf "${DIM}  [1-9] focus instance  ·  [r] refresh  ·  [q] quit  ·  auto-refresh 3s${R}\n"
}

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do
    render 2>/dev/null

    key=""
    if IFS= read -r -s -n 1 -t 3 key 2>/dev/null; then
        case "$key" in
            q|Q) exit 0 ;;
            r|R) continue ;;
            [1-9])
                nav_rows=()
                if [ -f "$ROWS_FILE" ]; then
                    while IFS= read -r line; do
                        [ -n "$line" ] && nav_rows+=("$line")
                    done < "$ROWS_FILE"
                fi
                nav_idx=$((key - 1))
                if [ "${#nav_rows[@]}" -gt 0 ] && [ "$nav_idx" -lt "${#nav_rows[@]}" ]; then
                    IFS=$'\t' read -r fsid fproc fcwd fpid faction fpane <<< "${nav_rows[$nav_idx]}"
                    focus_session "$fsid" "${fpane:-}"
                fi
                ;;
        esac
    fi
done

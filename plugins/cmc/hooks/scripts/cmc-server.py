#!/usr/bin/env python3
"""CMC — local dashboard server for active Claude Code sessions."""

import json
import os
import glob
import re
import secrets
import shlex
import subprocess
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

PORT = 7477
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
CONFIG_FILE  = os.path.expanduser("~/.claude/cmc.json")

# CSRF defense for the localhost dashboard: an unguessable per-process token the
# same-origin page reads from /api/token and replays as the X-CMC-Token header on
# every state-changing POST. A cross-origin page cannot read the token (no CORS),
# and the custom header forces a CORS preflight that fails cross-origin.
CMC_TOKEN = secrets.token_urlsafe(32)
_ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
# Host-header allowlist: a DNS-rebinding page (evil.com → 127.0.0.1) reaches the
# socket with Host: evil.com — reject it before serving the token or session data.
_ALLOWED_HOSTS = {
    f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost",
}
# Endpoints that launch a desktop app on a directory argument — cwd must be a real
# absolute path so it can never be interpreted as a CLI flag (argument injection).
_DIR_LAUNCH_PATHS = ("/api/action/fork", "/api/action/code", "/api/action/phpstorm")

# Shared JIRA ticket pattern (e.g. ABC-1234) and the worktree roots scanned when a
# session's repo has no ticket branch but its work lives in a sibling worktree
# (often a different repo). Override via cmc.json "worktreeGlobs".
JIRA_TICKET_RE = re.compile(r'([A-Z]+-\d+)')
DEFAULT_WORKTREE_GLOBS = ["~/Developer/*-worktrees/*"]

# ── Config helpers ────────────────────────────────────────────────────────────

def load_cmc_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cmc_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

# ── Session discovery ─────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=5):
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

_ANSI_RE = re.compile(r'\x1b\[[^A-Za-z]*[A-Za-z]')
# Claude Code working indicators (must appear as first char of a line):
#   · (U+00B7)  — standard status line: "· Swirling…"
#   ✶ (U+2736)  — alternate status line: "✶ Finagling…"
#   ✦ ✸         — other decorative variants seen in some versions
#   Braille     — fallback for older Claude Code builds
_WORKING_INDICATORS = set("·✶✦✸⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

def _working_line_label(line):
    """If line starts with a Claude working indicator, return the label text.

    Claude Code format: ✶ Finagling… (3m 46s · ↓ 7.0k tokens · thought for 3s)
    The indicator MUST be the first non-whitespace character — this prevents
    false positives when ✶ appears mid-sentence in terminal output.
    Returns label string, or '' if the line is not a working-status line.
    """
    clean = _ANSI_RE.sub("", line).strip()
    if not clean or clean[0] not in _WORKING_INDICATORS:
        return ""
    rest = clean[1:].strip()
    # Strip time/token details suffix: "(3m 46s · ↓ 7.0k tokens · …)"
    rest = re.sub(r'\s*\([^)]*\)\s*$', '', rest).strip()
    # Strip "esc to interrupt" suffix
    rest = re.sub(r'\s*esc to interrupt.*', '', rest, flags=re.IGNORECASE).strip()
    return rest[:40]

def get_pane_info(pane_target):
    """Single tmux capture → (action, working_label, last_command).

    Captures 100 lines of scrollback so last_command survives long responses.
    """
    out, rc = run_cmd(
        ["tmux", "capture-pane", "-p", "-S", "-100", "-t", pane_target], timeout=3
    )
    if rc != 0 or not out:
        return "none", "", "", ""

    lines = [l for l in out.splitlines() if l.strip()]
    recent = "\n".join(lines[-20:])
    low = recent.lower()

    # ── Status detection ───────────────────────────────────────────────────────
    action, working_label = "none", ""
    for line in reversed(lines[-20:]):
        clean = _ANSI_RE.sub("", line).strip()
        if clean and clean[0] in _WORKING_INDICATORS:
            action, working_label = "wait", _working_line_label(line)
            break
    if action == "none":
        if "esc to interrupt" in low:
            action = "wait"
        elif any(p in low for p in ("do you want to", "allow tool", "allow this",
                                     "[y/n]", "(y/n)", "proceed?")):
            action = "modal:approve"
        elif "what would you like" in low or ("[ ]" in recent and "todo" in low):
            action = "continue"
        elif any(p in low for p in ("error:", "failed:", "traceback", "exception:")):
            action = "review:error"

    # ── Last user command ──────────────────────────────────────────────────────
    # Claude Code marks submitted user input with ❯ at the start of the line.
    # Skip blank prompts and Claude Code hint lines.
    _SKIP_PATTERNS = (
        "press up to edit",
        "type a message",
        "queued messages",
    )
    last_command = ""
    for line in reversed(lines):
        clean = _ANSI_RE.sub("", line).strip()
        if clean.startswith("❯"):
            content = clean[1:].replace("\xa0", " ").strip()
            if content and not any(p in content.lower() for p in _SKIP_PATTERNS):
                last_command = content[:55] + ("…" if len(content) > 55 else "")
                break

    # Bottom-most JIRA ticket visible in the pane — hint for locating a cross-repo
    # worktree when the session's own repo has no ticket branch.
    ticket_hint = ""
    for line in reversed(lines):
        m = JIRA_TICKET_RE.search(_ANSI_RE.sub("", line))
        if m:
            ticket_hint = m.group(1)
            break

    return action, working_label, last_command, ticket_hint

def get_pane_current_path(pane_target):
    """Return the tmux pane's live current path (ground truth of where the pane
    is right now), or "" if unavailable."""
    if not pane_target:
        return ""
    out, rc = run_cmd(
        ["tmux", "display", "-p", "-t", pane_target, "#{pane_current_path}"], timeout=3
    )
    return out if rc == 0 and out else ""

def scan_global_worktrees(globs=None):
    """Return [(ticket, path, mtime)] for git worktrees under the configured roots
    whose branch name contains a JIRA ticket. Used to locate cross-repo worktrees
    (e.g. a session launched in one repo doing work in a sibling worktree)."""
    if globs is None:
        cfg = load_cmc_config()
        globs = cfg.get("worktreeGlobs") or DEFAULT_WORKTREE_GLOBS
    results, seen = [], set()
    for pattern in globs:
        for path in glob.glob(os.path.expanduser(pattern)):
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            branch, rc = run_cmd(
                ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], timeout=3
            )
            if rc != 0 or not branch or branch == "HEAD":
                continue
            m = JIRA_TICKET_RE.search(branch)
            if not m:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            results.append((m.group(1), path, mtime))
    return results

def get_jira_and_working_dir(cwd, ticket_hint=None, global_worktrees=None):
    """Return (jira_ticket, working_dir) for a session.

    1. If the session CWD is on a JIRA branch → working_dir = cwd.
    2. If the pane shows a ticket (ticket_hint), point at the matching worktree —
       first among the cwd repo's own worktrees, then among the configured global
       roots (handles the cross-repo case, e.g. a session in one repo whose work
       lives in a sibling repo's worktree).
    Worktree selection is driven by the ticket the pane is actually showing, never
    by "first worktree found" — so a ticketless session is never mis-pointed at an
    unrelated worktree. working_dir is the root to open in Fork / Code / PHPStorm.
    """
    if not cwd or cwd == "/":
        return None, cwd

    # 1. Check HEAD branch of the session's cwd
    out, rc = run_cmd(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=3)
    if rc == 0 and out and out != "HEAD":
        m = JIRA_TICKET_RE.search(out)
        if m:
            return m.group(1), cwd  # CWD is directly on the ticket branch

    # Without a ticket hint we can't know which worktree this session belongs to.
    if not ticket_hint:
        return None, cwd

    # 2a. A worktree of the cwd repo on the hinted ticket's branch.
    wt_out, rc = run_cmd(["git", "-C", cwd, "worktree", "list"], timeout=3)
    if rc == 0 and wt_out:
        for line in wt_out.splitlines():
            # Format: /path/to/worktree  abc1234 [branch-name]
            parts = line.split()
            if not parts:
                continue
            wt_path = parts[0]
            branch_match = re.search(r'\[([^\]]+)\]', line)
            if branch_match and os.path.isdir(wt_path):
                jm = JIRA_TICKET_RE.search(branch_match.group(1))
                if jm and jm.group(1) == ticket_hint:
                    return ticket_hint, wt_path

    # 2b. A worktree under the configured global roots (cross-repo case).
    if global_worktrees is None:
        global_worktrees = scan_global_worktrees()
    matches = [(p, mt) for (t, p, mt) in global_worktrees if t == ticket_hint]
    if matches:
        # Newest by mtime if a ticket somehow has multiple worktrees.
        return ticket_hint, max(matches, key=lambda x: x[1])[0]

    return None, cwd

def get_active_pane():
    """Return the tmux pane target currently focused by the active client."""
    out, rc = run_cmd(
        ["tmux", "list-panes", "-a", "-F",
         "#{pane_active}\t#{window_active}\t#{session_name}:#{window_index}.#{pane_index}"],
        timeout=3,
    )
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] == "1" and parts[1] == "1":
            return parts[2].strip()
    return ""

def get_sessions():
    ppid_of = {}
    ps_out, _ = run_cmd(["ps", "-eo", "pid,ppid"])
    for line in ps_out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2:
            ppid_of[parts[0]] = parts[1]

    tmux_by_pid = {}
    tmux_out, rc = run_cmd(["tmux", "list-panes", "-a",
                            "-F", "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}"])
    if rc == 0 and tmux_out:
        for line in tmux_out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                tmux_by_pid[parts[0].strip()] = parts[1].strip()

    active_pane = get_active_pane()
    global_worktrees = scan_global_worktrees()  # scanned once per refresh

    sessions = []
    for path in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json"))):
        try:
            with open(path) as f:
                data = json.load(f)
            pid = str(data.get("pid", ""))
            if not pid or not is_alive(pid):
                continue
            started_at = data.get("startedAt", 0)
            ppid = ppid_of.get(pid, "")
            pane_target = tmux_by_pid.get(ppid, "") if ppid else ""
            # Only show sessions in a tmux pane
            if not pane_target:
                continue
            # Prefer the pane's live current path over the frozen session-launch cwd.
            cwd = get_pane_current_path(pane_target) or data.get("cwd", "")
            action, working_label, last_command, ticket_hint = get_pane_info(pane_target)
            jira, working_dir = get_jira_and_working_dir(cwd, ticket_hint, global_worktrees)
            sessions.append({
                "pid": pid,
                "cwd": cwd,
                "working_dir": working_dir,
                "startedAt": started_at,
                "pane": pane_target,
                "action": action,
                "label": working_label,
                "last_command": last_command,
                "jira": jira,
                "focused": pane_target == active_pane,
            })
        except Exception:
            continue

    sessions.sort(key=lambda s: s.get("startedAt", 0))
    return sessions

def focus_tmux_pane(pane_target):
    """Select a tmux pane/window and bring the hosting iTerm2 session to front."""
    # 1. Select pane + window in tmux
    window = pane_target.rsplit(".", 1)[0]   # "DEV:1.2" → "DEV:1"
    run_cmd(["tmux", "select-pane",   "-t", pane_target])
    run_cmd(["tmux", "select-window", "-t", window])

    # 2. Find which TTY the tmux client is on
    tmux_session = pane_target.split(":")[0]  # "DEV:1.2" → "DEV"
    clients_out, rc = run_cmd(
        ["tmux", "list-clients", "-t", tmux_session, "-F", "#{client_tty}"]
    )
    client_tty = ""
    if rc == 0 and clients_out:
        raw = clients_out.splitlines()[0].strip()
        client_tty = raw.lstrip("/dev/")   # "/dev/ttys005" → "ttys005"

    # 3. Match TTY to an iTerm2 session UUID
    it2_sid = ""
    if client_tty:
        sessions_json, _ = run_cmd(["it2", "session", "list", "--format", "json"])
        try:
            for s in json.loads(sessions_json or "[]"):
                shell_pid = str(s.get("ShellPID", ""))
                if not shell_pid:
                    continue
                tty_out, _ = run_cmd(["ps", "-p", shell_pid, "-o", "tty="])
                if tty_out.strip() == client_tty:
                    it2_sid = s.get("SessionID", "")
                    break
        except Exception:
            pass

    # 4. Focus the specific iTerm2 session by UUID, or fall back to activate
    if it2_sid:
        script = "\n".join([
            'tell application "iTerm2"',
            '  repeat with w in windows',
            '    repeat with t in tabs of w',
            '      repeat with s in sessions of t',
            f'        if (unique ID of s) is "{it2_sid}" then',
            '          select w',
            '          tell t to select',
            '          select s',
            '          activate',
            '          return',
            '        end if',
            '      end repeat',
            '    end repeat',
            '  end repeat',
            'end tell',
        ])
    else:
        script = 'tell application "iTerm2" to activate'

    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

# ── Terminal (TUI) renderer ───────────────────────────────────────────────────

# ANSI codes
_R  = "\033[0m"
_B  = "\033[1m"
_D  = "\033[2m"
_G  = "\033[32m"   # green
_C  = "\033[36m"   # cyan
_BL = "\033[34m"   # blue
_Y  = "\033[33m"   # yellow
_RE = "\033[91m"   # red

W = 44  # inner tile width (chars)

_STATUS_TUI = {
    "wait":          (_G,  "● Working"),
    "continue":      (_C,  "● Has Todos"),
    "modal:approve": (_Y,  "⚠ Needs Approval"),
    "modal:review":  (_Y,  "⚠ Review"),
    "review:error":  (_RE, "✗ Error"),
    "none":          (_BL, "○ Idle"),
    "dead":          (_D,  "  Dead"),
}

def _dur(started_at):
    if not started_at:
        return ""
    # startedAt is milliseconds since epoch
    elapsed = int(time.time() - started_at / 1000)
    if elapsed < 0:
        return ""
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    return f"{elapsed // 3600}h{(elapsed % 3600) // 60}m"

def render_tui():
    sessions = get_sessions()
    attention = sum(
        1 for s in sessions
        if s["action"] in ("modal:approve", "modal:review", "review:error")
    )
    now = datetime.now().strftime("%H:%M:%S")
    hr = "═" * W

    out = []
    # Header
    out.append(f"{_B}{_C}╔{hr}╗{_R}")
    h1 = f"  🤖  CLAUDE OVERLORD"
    h1_line = f"{h1:<{W - len(now) - 2}}{now}  "
    out.append(f"{_B}{_C}║{_R}{_B}{h1_line}{_C}║{_R}")
    h2 = f"  Sessions: {len(sessions)}   Attention: {attention}"
    out.append(f"{_B}{_C}║{_R}{_D}{h2:<{W}}{_C}║{_R}")
    out.append(f"{_B}{_C}╚{hr}╝{_R}")
    out.append("")

    if not sessions:
        out.append(f"{_D}  No active Claude Code sessions{_R}")
        out.append(f"{_D}  Watching for new sessions...{_R}")
        return "\n".join(out) + "\n"

    thr = "─" * W
    for i, s in enumerate(sessions):
        color, status_label = _STATUS_TUI.get(s["action"], _STATUS_TUI["none"])
        # Prefer the live working label over the generic "Working" text
        if s["action"] == "wait" and s.get("label"):
            status_label = s["label"]
        proj = os.path.basename(s["cwd"]) or s["cwd"]
        jira = s.get("jira")
        short_cwd = s["cwd"].replace(os.path.expanduser("~"), "~")
        dur = _dur(s.get("startedAt", 0))
        pane_info = f"pane {s['pane']}  ·  pid {s['pid']}"

        # Title: JIRA ticket if available, else project name
        title = f"{jira}  ·  {proj}" if jira else proj

        # Row 1: [n] title + status (right-aligned)
        right = f" {status_label}  "
        left = f" [{i+1}] {title}"
        if len(left) + len(right) > W:
            left = left[:W - len(right) - 1] + "…"
        gap = W - len(left) - len(right)
        r1 = left + (" " * max(gap, 1)) + right

        # Row 2: path (truncated)
        if len(short_cwd) > W - 2:
            short_cwd = "…" + short_cwd[-(W - 3):]
        r2 = f"  {short_cwd}"

        # Row 3: pane/pid + duration (right-aligned)
        r3_left = f"  {pane_info}"
        r3 = r3_left + (" " * max(W - len(r3_left) - len(dur) - 2, 1)) + dur + "  "

        # Row 4: last user command (truncated, in dim style)
        last_cmd = s.get("last_command", "")
        r4 = f"  \"{last_cmd}\"" if last_cmd else ""

        out.append(f"{color}{_B}┌{thr}┐{_R}")
        out.append(f"{color}│{_R}{_B}{r1:<{W}}{color}│{_R}")
        out.append(f"{color}│{_R}{_D}{r2:<{W}}{color}│{_R}")
        out.append(f"{color}│{_R}{_D}{r3:<{W}}{color}│{_R}")
        if r4:
            out.append(f"{color}│{_R}{_D}{r4:<{W}}{color}│{_R}")
        out.append(f"{color}{_B}└{thr}┘{_R}")
        out.append("")

    out.append(f"{_D}  [1-9] focus pane  ·  [q] quit  ·  auto-refresh 3s{_R}")
    return "\n".join(out) + "\n"

# ── Dashboard HTML ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMC</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0d0d12;
  --surface: #16161f;
  --border: #32324a;
  --text: #e8e8f2;
  --muted: #9090b8;
  --dim: #5a5a7a;
}
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", ui-sans-serif, sans-serif;
  font-size: 12px;
  padding: 0 8px 16px;
  overflow-x: hidden;
  min-height: 100vh;
}
header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 4px 9px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 10px;
  position: sticky;
  top: 0;
  background: var(--bg);
  z-index: 10;
}
.logo { font-size: 15px; line-height: 1; }
.title { font-weight: 700; font-size: 13px; color: #fff; letter-spacing: -0.3px; }
.hcount { margin-left: auto; font-size: 11px; color: var(--muted); }
.saa-toggle {
  display: flex; align-items: center; gap: 3px;
  border-radius: 10px; padding: 3px 8px;
  font-size: 11px; font-weight: 700;
  cursor: pointer; transition: filter .15s;
  margin-left: 4px; flex-shrink: 0;
  user-select: none;
}
.saa-toggle.off {
  background: #1a1a2a; color: var(--dim);
  border: 1px solid var(--border);
}
.saa-toggle.on {
  background: #0a3d1f; color: #5edb8a;
  border: 1px solid #1e6632;
}
.saa-toggle:hover { filter: brightness(1.35); }
.saa-toggle:active { filter: brightness(.7); }
.saa-icon { font-size: 12px; line-height: 1; }
.attn-badge {
  background: #f59e0b; color: #000;
  border-radius: 8px; padding: 1px 7px;
  font-size: 10px; font-weight: 800;
}

.tile {
  border-radius: 10px;
  border: 1px solid var(--border);
  padding: 11px 12px 10px 26px;
  margin-bottom: 8px;
  background: var(--surface);
  transition: border-color .3s, box-shadow .3s;
  cursor: pointer;
  position: relative;
}
.tile:hover { background: #1c1c28; }
.tile.dragging { opacity: 0.4; }
.tile.drag-over { border-top: 2px solid #a5b4fc; margin-top: -1px; }
.drag-handle {
  position: absolute;
  left: 7px; top: 50%;
  transform: translateY(-50%);
  cursor: grab;
  color: var(--dim);
  font-size: 13px;
  line-height: 1;
  user-select: none;
  opacity: 0;
  transition: opacity .15s;
}
.tile:hover .drag-handle { opacity: 1; }
.drag-handle:active { cursor: grabbing; }
.pane-badge {
  position: absolute;
  top: 8px; right: 10px;
  font-size: 9px; font-weight: 700;
  color: var(--dim);
  letter-spacing: .3px;
  font-variant-numeric: tabular-nums;
}
.tile-wait     { border-color: #22863a; }
.tile-continue { border-color: #1a6878; }
.tile-approve  { border-color: #b45309; animation: glow-amber 1.8s ease-in-out infinite; }
.tile-review   { border-color: #c2410c; animation: glow-orange 1.8s ease-in-out infinite; }
.tile-error    { border-color: #c53030; animation: glow-red 1.8s ease-in-out infinite; }
.tile-idle     { border-color: var(--border); }
.tile-dead     { border-color: #2a2a3a; opacity: .4; }
.tile-focused  { border-color: #4d9ef9 !important; box-shadow: 0 0 0 1px #4d9ef9; }

@keyframes glow-amber  { 50% { box-shadow: 0 0 10px rgba(245,158,11,.3); border-color: #d97706; } }
@keyframes glow-orange { 50% { box-shadow: 0 0 10px rgba(249,115,22,.3); border-color: #ea580c; } }
@keyframes glow-red    { 50% { box-shadow: 0 0 10px rgba(239,68,68,.3);  border-color: #dc2626; } }

.project {
  font-weight: 600; font-size: 14px; color: #fff;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.cwd-path {
  font-size: 10px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-top: 2px;
}
.meta {
  display: flex; align-items: center; gap: 6px;
  margin-top: 8px;
}
.badge {
  border-radius: 5px; padding: 2px 6px;
  font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .4px;
  flex-shrink: 0;
}
.b-wait     { background:#0a3d1f; color:#5edb8a; }
.b-continue { background:#072d3d; color:#38d9f5; }
.b-approve  { background:#3d2200; color:#fcd34d; }
.b-review   { background:#3d1500; color:#fb8c4a; }
.b-error    { background:#3d0a0a; color:#fc8181; }
.b-idle     { background:#151530; color:#a5b4fc; }
.b-dead     { background:#1a1a22; color:#666680; }

.duration { margin-left: auto; font-size: 10px; color: var(--dim); flex-shrink: 0; }
.info-row { font-size: 10px; color: var(--dim); margin-top: 4px; font-variant-numeric: tabular-nums; }
.last-cmd {
  font-size: 10px; color: var(--muted);
  margin-top: 5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-style: italic;
  opacity: 0.75;
}
.actions  { display: flex; gap: 6px; margin-top: 10px; }
.btn {
  flex: 1; border: none; border-radius: 7px;
  padding: 7px 6px; font-size: 11px; font-weight: 600;
  cursor: pointer; display: flex; align-items: center;
  justify-content: center; gap: 4px;
  transition: filter .15s; letter-spacing: -0.1px;
}
.btn:hover  { filter: brightness(1.25); }
.btn:active { filter: brightness(.7); }
.btn-fork    { background: #0f2d12; color: #5edb8a; border: 1px solid #1e6632; }
.btn-code    { background: #131340; color: #a5b4fc; border: 1px solid #3730a3; }
.btn-phpstorm { background: #1f0a32; color: #c084fc; border: 1px solid #7c3aed; }

.empty {
  text-align: center; color: var(--muted);
  padding: 50px 0; font-size: 12px;
}
.empty .icon { font-size: 28px; margin-bottom: 10px; }
.footer { text-align: center; color: var(--dim); font-size: 10px; padding: 8px 0 2px; }
</style>
</head>
<body>
<header>
  <span class="logo">&#x1F916;</span>
  <span class="title">CMC</span>
  <button class="saa-toggle off" id="saaToggle" title="Safe Auto-Accept (off)">
    <span class="saa-icon">&#x1F6E1;&#xFE0F;</span>
    <span>Auto</span>
  </button>
  <span class="hcount" id="hcount">&#8212;</span>
</header>
<div id="root"></div>
<div class="footer" id="footer">&#8212;</div>
<script>
/* All dynamic content is set via textContent — no innerHTML with untrusted data. */

var STATUS = {
  "wait":          { label: "Working",        cls: "wait",     badge: "b-wait"     },
  "continue":      { label: "Has Todos",      cls: "continue", badge: "b-continue" },
  "modal:approve": { label: "Needs Approval", cls: "approve",  badge: "b-approve"  },
  "modal:review":  { label: "Review",         cls: "review",   badge: "b-review"   },
  "review:error":  { label: "Error",          cls: "error",    badge: "b-error"    },
  "none":          { label: "Idle",           cls: "idle",     badge: "b-idle"     },
  "dead":          { label: "Dead",           cls: "dead",     badge: "b-dead"     }
};

/* ── Safe Auto-Accept toggle ─────────────────────────────────────────────── */
var _saaEnabled = false;

function updateSaaToggle(enabled) {
  _saaEnabled = !!enabled;
  var btn = document.getElementById("saaToggle");
  if (!btn) return;
  btn.className = "saa-toggle " + (_saaEnabled ? "on" : "off");
  btn.title = "Safe Auto-Accept: " + (_saaEnabled
    ? "ON — MySQL SELECTs and gh reads are auto-approved"
    : "OFF — all tool calls require normal review");
}

var CSRF_TOKEN = "";
function loadToken() {
  return fetch("/api/token")
    .then(function(r) { return r.json(); })
    .then(function(d) { CSRF_TOKEN = d.token || ""; })
    .catch(function() {});
}

function loadConfig() {
  fetch("/api/config")
    .then(function(r) { return r.json(); })
    .then(function(cfg) { updateSaaToggle(cfg.safe_auto_accept); })
    .catch(function() {});
}

function toggleSaa(e) {
  e.stopPropagation();
  fetch("/api/action/toggle_safe_accept", { method: "POST", headers: { "X-CMC-Token": CSRF_TOKEN } })
    .then(function(r) { return r.json(); })
    .then(function(cfg) { updateSaaToggle(cfg.safe_auto_accept); })
    .catch(function() {});
}

function dur(ts) {
  if (!ts) return "";
  /* startedAt is milliseconds since epoch */
  var s = Math.floor((Date.now() - ts) / 1000);
  if (s < 0) return "";
  if (s < 60)   return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

function projName(cwd) {
  var parts = (cwd || "").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "unknown";
}

function shortPath(cwd) {
  return (cwd || "").replace(/^\/Users\/[^/]+/, "~");
}

function el(tag, cls) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

/* ── Drag-and-drop order ─────────────────────────────────────────────────── */
function loadOrder() {
  try { return JSON.parse(localStorage.getItem("overlord-order") || "[]"); }
  catch(e) { return []; }
}
function saveOrder(panes) {
  localStorage.setItem("overlord-order", JSON.stringify(panes));
}
function sortSessions(sessions) {
  var order = loadOrder();
  if (!order.length) return sessions;
  var result = [];
  order.forEach(function(pane) {
    var s = sessions.find(function(x) { return x.pane === pane; });
    if (s) result.push(s);
  });
  sessions.forEach(function(s) {
    if (order.indexOf(s.pane) === -1) result.push(s);
  });
  return result;
}

var _dragSrcPane = null;
var _dragging    = false;

function doAction(type, cwd, extra) {
  var url = "/api/action/" + type + "?cwd=" + encodeURIComponent(cwd);
  if (extra) url += "&" + extra;
  fetch(url, { method: "POST", headers: { "X-CMC-Token": CSRF_TOKEN } }).catch(function() {});
}

function makeTile(s) {
  var st = STATUS[s.action] || STATUS["none"];

  var tileClass = "tile tile-" + st.cls + (s.focused ? " tile-focused" : "");
  var tile = el("div", tileClass);
  tile.dataset.pane = s.pane;
  tile.setAttribute("draggable", "true");

  // ── Drag handle (grip dots, visible on hover) ───────────────────────────
  var handle = el("div", "drag-handle");
  handle.textContent = "\u283F"; // ⠿ braille 2×3 dot grid
  handle.addEventListener("click", function(e) { e.stopPropagation(); });
  tile.appendChild(handle);

  // ── Drag events ─────────────────────────────────────────────────────────
  tile.addEventListener("dragstart", function(e) {
    _dragSrcPane = s.pane;
    _dragging    = true;
    tile.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  tile.addEventListener("dragend", function() {
    tile.classList.remove("dragging");
    document.querySelectorAll(".tile").forEach(function(t) {
      t.classList.remove("drag-over");
    });
    setTimeout(function() { _dragging = false; }, 50);
  });
  tile.addEventListener("dragover", function(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (_dragSrcPane !== s.pane) tile.classList.add("drag-over");
  });
  tile.addEventListener("dragleave", function() {
    tile.classList.remove("drag-over");
  });
  tile.addEventListener("drop", function(e) {
    e.preventDefault();
    tile.classList.remove("drag-over");
    if (!_dragSrcPane || _dragSrcPane === s.pane) return;
    // Compute new order from current DOM, then move src before drop target
    var panes = Array.from(document.querySelectorAll(".tile"))
                     .map(function(t) { return t.dataset.pane; })
                     .filter(Boolean);
    var si = panes.indexOf(_dragSrcPane);
    var di = panes.indexOf(s.pane);
    if (si !== -1) {
      panes.splice(si, 1);
      var newDi = panes.indexOf(s.pane);
      panes.splice(newDi !== -1 ? newDi : di, 0, _dragSrcPane);
    }
    saveOrder(panes);
    _dragSrcPane = null;
    refresh();
  });

  // ── Click → focus pane (skip if drag just ended) ────────────────────────
  tile.addEventListener("click", function(e) {
    if (_dragging) return;
    if (e.target.closest(".btn") || e.target.closest(".drag-handle")) return;
    if (s.pane) {
      doAction("focus", "", "pane=" + encodeURIComponent(s.pane));
    }
  });

  // Pane badge (top-right corner): e.g. "DEV 1·2"
  if (s.pane) {
    var paneBadge = el("div", "pane-badge");
    // "DEV:1.2" → "DEV  1·2"
    paneBadge.textContent = s.pane.replace(":", "  ").replace(".", "\xB7");
    tile.appendChild(paneBadge);
  }

  // Title: JIRA ticket if present, else project name
  var titleText = s.jira ? s.jira + "  \xB7  " + projName(s.cwd) : projName(s.cwd);
  var project = el("div", "project");
  project.textContent = titleText;
  tile.appendChild(project);

  var cwdDiv = el("div", "cwd-path");
  cwdDiv.textContent = shortPath(s.cwd);
  tile.appendChild(cwdDiv);

  var meta = el("div", "meta");
  var badge = el("span", "badge " + st.badge);
  badge.textContent = (s.action === "wait" && s.label) ? s.label : st.label;
  var duration = el("span", "duration");
  duration.textContent = dur(s.startedAt);
  meta.appendChild(badge);
  meta.appendChild(duration);
  tile.appendChild(meta);

  var info = el("div", "info-row");
  info.textContent = (s.pane ? "pane " + s.pane + "  \xB7  " : "") + "pid " + s.pid;
  tile.appendChild(info);

  if (s.last_command) {
    var lastCmd = el("div", "last-cmd");
    lastCmd.textContent = "\u201C" + s.last_command + "\u201D";
    tile.appendChild(lastCmd);
  }

  var actions = el("div", "actions");

  // Use working_dir (the active worktree root) when available so Fork / Code /
  // PHPStorm open the right directory even when Claude started at the repo root.
  var wd = s.working_dir || s.cwd;

  var btnFork = el("button", "btn btn-fork");
  btnFork.textContent = "\u2325 Fork";
  btnFork.addEventListener("click", function() { doAction("fork", wd); });

  var btnCode = el("button", "btn btn-code");
  btnCode.textContent = "</> Code";
  btnCode.addEventListener("click", function() { doAction("code", wd); });

  var btnPhp = el("button", "btn btn-phpstorm");
  btnPhp.textContent = "\u03c6 PHPStorm";
  btnPhp.addEventListener("click", function() { doAction("phpstorm", wd); });

  actions.appendChild(btnFork);
  actions.appendChild(btnCode);
  actions.appendChild(btnPhp);
  tile.appendChild(actions);

  return tile;
}

function refresh() {
  fetch("/api/sessions")
    .then(function(r) { return r.json(); })
    .then(function(sessions) {
      var attn = sessions.filter(function(s) {
        return s.action && (s.action.indexOf("modal:") === 0 || s.action.indexOf("review:") === 0);
      }).length;

      var hcount = document.getElementById("hcount");
      if (attn > 0) {
        hcount.textContent = "";
        var badge = el("span", "attn-badge");
        badge.textContent = "\u26A0 " + attn + " need" + (attn === 1 ? "s" : "") + " attention";
        hcount.appendChild(badge);
      } else {
        hcount.textContent = sessions.length + (sessions.length === 1 ? " session" : " sessions");
      }

      var root = document.getElementById("root");
      while (root.firstChild) root.removeChild(root.firstChild);

      if (!sessions.length) {
        var empty = el("div", "empty");
        var icon = el("div", "icon");
        icon.textContent = "\uD83E\uDD16";
        var msg = document.createTextNode("No active sessions");
        empty.appendChild(icon);
        empty.appendChild(msg);
        root.appendChild(empty);
      } else {
        sortSessions(sessions).forEach(function(s) { root.appendChild(makeTile(s)); });
      }

      document.getElementById("footer").textContent =
        "auto-refresh \xB7 " + new Date().toLocaleTimeString();
    })
    .catch(function(e) {
      document.getElementById("footer").textContent = "error: " + e.message;
    });
}

document.getElementById("saaToggle").addEventListener("click", toggleSaa);
loadToken();
loadConfig();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _host_ok(self):
        return self.headers.get("Host", "") in _ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            self.send_response(403)
            self.end_headers()
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/tui":
            body = render_tui().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/sessions":
            data = json.dumps(get_sessions()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/api/config":
            data = json.dumps(load_cmc_config()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/api/token":
            # Same-origin only by virtue of no CORS header — a cross-origin page
            # can issue the request but cannot read the token from the response.
            body = json.dumps({"token": CMC_TOKEN}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _csrf_ok(self):
        """Reject state-changing requests lacking the per-process token or coming
        from a foreign origin."""
        if not self._host_ok():
            return False
        token = self.headers.get("X-CMC-Token", "")
        if not secrets.compare_digest(token, CMC_TOKEN):
            return False
        origin = self.headers.get("Origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            return False
        return True

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._csrf_ok():
            self.send_response(403)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        cwd = unquote(qs.get("cwd", [""])[0])

        # Directory-launch actions: cwd must be a real absolute directory so it can
        # never be parsed as a CLI flag by the launched app (argument injection).
        if parsed.path in _DIR_LAUNCH_PATHS:
            if not (cwd.startswith("/") and os.path.isdir(cwd)):
                self.send_response(400)
                self.end_headers()
                return

        if parsed.path == "/api/action/focus":
            pane = unquote(qs.get("pane", [""])[0])
            idx_str = qs.get("idx", [""])[0]
            if idx_str and not pane:
                try:
                    sessions = get_sessions()
                    idx = int(idx_str) - 1
                    if 0 <= idx < len(sessions):
                        pane = sessions[idx].get("pane", "")
                except (ValueError, IndexError):
                    pass
            if pane:
                focus_tmux_pane(pane)
        elif parsed.path == "/api/action/fork" and cwd:
            subprocess.Popen(
                ["open", "-a", "Fork", cwd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif parsed.path == "/api/action/code" and cwd:
            try:
                subprocess.Popen(
                    ["code", cwd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                subprocess.Popen(
                    ["open", "-a", "Visual Studio Code", cwd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        elif parsed.path == "/api/action/phpstorm" and cwd:
            try:
                subprocess.Popen(
                    ["pstorm", cwd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                subprocess.Popen(
                    ["open", "-a", "PhpStorm", cwd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        elif parsed.path == "/api/action/toggle_safe_accept":
            cfg = load_cmc_config()
            cfg["safe_auto_accept"] = not cfg.get("safe_auto_accept", False)
            save_cmc_config(cfg)
            body = json.dumps(cfg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass  # suppress request logs

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CMC listening on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

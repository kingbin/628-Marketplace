#!/usr/bin/env python3
"""
CMC Safe Auto-Accept — PreToolUse hook.

Auto-approves read-only operations when safe_auto_accept is enabled in
~/.claude/cmc.json. Destructive operations (writes, mutations) are never
auto-approved and fall back to the normal Claude Code permission flow.

Safe to auto-approve:
  - mcp__mysql__mysql_select_data / mysql_list_tables / mysql_describe_table
  - Bash: mysql commands without write keywords (INSERT/UPDATE/DELETE/etc.)
  - Bash: gh read operations (pr list/view/diff/checks, issue list/view, etc.)
  - Bash: git read operations (pull, fetch, log, status, diff, show, etc.)

Always require review:
  - MySQL: INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, CREATE, REPLACE
  - gh: pr create/merge/close/edit, issue create/close/edit, release create
  - git push / gh push
"""
import json
import os
import re
import shlex
import sys

CONFIG_FILE = os.path.expanduser("~/.claude/cmc.json")

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

# ── Safety classifiers ────────────────────────────────────────────────────────

_MYSQL_WRITE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|GRANT|REVOKE)\b',
    re.IGNORECASE,
)

_GH_DESTRUCTIVE = re.compile(
    r'gh\s+('
    r'pr\s+(create|merge|close|edit|review\s)|'
    r'issue\s+(create|close|edit)|'
    r'release\s+create|'
    r'workflow\s+run'
    r')',
    re.IGNORECASE,
)
_GH_SAFE = re.compile(
    r'gh\s+('
    r'pr\s+(list|view|diff|checks|status)|'
    r'issue\s+(list|view)|'
    r'repo\s+(view|list)|'
    r'run\s+(list|view)|'
    r'release\s+(list|view)|'
    r'api\s+'
    r')',
    re.IGNORECASE,
)

_GIT_PUSH = re.compile(r'git\s+push', re.IGNORECASE)
_GIT_SAFE = re.compile(
    r'git\s+(pull|fetch|log|status|diff|show|branch|stash\s+list|remote\s+show|tag\b)',
    re.IGNORECASE,
)


# Shell punctuation that, when surfaced as its own token by shlex, signals command
# chaining / redirection (; & | < > and the () subshell chars).
_PUNCT_CHARS = set("();<>|&")


def _safe_argv(command: str):
    """Tokenize a Bash command and return its argv ONLY if it is a single, simple
    command — no command/parameter substitution and no shell operators. Returns
    None (treated as unsafe) otherwise. This is the gate that stops a safe-looking
    prefix (e.g. ``git log``) from smuggling a second command (``; rm -rf ~``)."""
    # Command/parameter substitution is dangerous even inside double quotes.
    if "`" in command or "$(" in command or "${" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None  # unbalanced quotes, etc.
    if not tokens:
        return None
    for tok in tokens:
        # A token composed purely of punctuation is a shell operator surfaced by
        # punctuation_chars (quoted operators stay embedded in their token).
        if tok and set(tok) <= _PUNCT_CHARS:
            return None
    return tokens


def is_safe_bash(command: str) -> bool:
    """Return True if a Bash command is safe to auto-approve."""
    argv = _safe_argv(command)
    if not argv:
        return False
    binary = os.path.basename(argv[0])

    # MySQL: allow unless write keywords are present
    if binary == "mysql":
        return not _MYSQL_WRITE.search(command)

    # gh CLI: block destructive, allow explicit read operations
    if binary == "gh":
        if _GH_DESTRUCTIVE.search(command) or _GIT_PUSH.search(command):
            return False
        return bool(_GH_SAFE.search(command))

    # git: read-only operations only
    if binary == "git":
        if _GIT_PUSH.search(command):
            return False
        return bool(_GIT_SAFE.search(command))

    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not load_config().get("safe_auto_accept", False):
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    approved = False

    # MCP MySQL read-only tools — always safe
    if tool_name in (
        "mcp__mysql__mysql_select_data",
        "mcp__mysql__mysql_list_tables",
        "mcp__mysql__mysql_describe_table",
    ):
        approved = True

    # Bash — inspect the command
    elif tool_name == "Bash":
        approved = is_safe_bash(tool_input.get("command", ""))

    if approved:
        print(json.dumps({
            "hookSpecificOutput": {"permissionDecision": "allow"},
            "systemMessage": "CMC Safe Auto-Accept: read-only operation approved automatically.",
        }))

    sys.exit(0)


if __name__ == "__main__":
    main()

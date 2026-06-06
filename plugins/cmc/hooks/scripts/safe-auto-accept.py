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
  - Bash: gh api GET/HEAD only (no method override, no field/body data)
  - Bash: git read operations (pull, fetch, log, status, diff, show, etc.)

Always require review (never auto-approved):
  - MySQL: INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, CREATE, REPLACE
  - gh: anything not on the positional read allowlist; gh api with -X/--method
    or -f/-F/--field/--raw-field/--input (i.e. any mutation)
  - git: anything not on the positional read allowlist; git push; any global
    option before the subcommand (blocks `git -c …` config-exec injection);
    --upload-pack / --receive-pack / --exec transport-exec injection
  - Any multi-line command, command/parameter substitution, or shell operator

Validation is positional (argv-based), not substring matching, so a safe-looking
prefix cannot smuggle a different command (e.g. `git commit -m "git log"`), and a
newline cannot chain a second command past the operator check.
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

# Positional read-only allowlists: (subcommand, action) for gh; subcommand for
# git. Validated against argv, never substring-matched against the raw command.
_GH_SAFE_SUB = {
    ("pr", "list"), ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "status"),
    ("issue", "list"), ("issue", "view"),
    ("repo", "view"), ("repo", "list"),
    ("run", "list"), ("run", "view"),
    ("release", "list"), ("release", "view"),
}
# gh api flags that turn a read into a write (method override / body / fields).
_GH_API_MUTATING = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}

_GIT_SAFE_SUB = {"pull", "fetch", "log", "status", "diff", "show", "branch", "tag"}
# git flags that can execute arbitrary commands via the transport/exec machinery.
_GIT_EXEC_FLAGS = ("--upload-pack", "--receive-pack", "--exec")


def _flag_key(tok: str) -> str:
    """Normalize a flag token to its option name (strip an ``=value`` suffix)."""
    return tok.split("=", 1)[0]


def _is_safe_gh(argv) -> bool:
    """argv[0] is gh. Allow only positional read subcommands, and gh api only as a
    plain GET/HEAD (no method override, no field/body data)."""
    if len(argv) < 2:
        return False
    sub = argv[1]
    if sub == "api":
        for tok in argv[2:]:
            if _flag_key(tok) in _GH_API_MUTATING:
                return False
        return True
    if len(argv) < 3:
        return False
    return (sub, argv[2]) in _GH_SAFE_SUB


def _is_safe_git(argv) -> bool:
    """argv[0] is git. Require the subcommand immediately at argv[1] (no global
    option may precede it — blocks ``git -c core.pager=… log`` config-exec
    injection) and block transport-exec flag injection on pull/fetch."""
    if len(argv) < 2 or argv[1].startswith("-"):
        return False
    sub, rest = argv[1], argv[2:]
    if sub == "stash":
        return bool(rest) and rest[0] == "list"
    if sub == "remote":
        return bool(rest) and rest[0] == "show"
    if sub in _GIT_SAFE_SUB:
        return not any(_flag_key(tok) in _GIT_EXEC_FLAGS for tok in rest)
    return False


# Shell punctuation that, when surfaced as its own token by shlex, signals command
# chaining / redirection (; & | < > and the () subshell chars).
_PUNCT_CHARS = set("();<>|&")


def _safe_argv(command: str):
    """Tokenize a Bash command and return its argv ONLY if it is a single, simple
    command — no newlines, no command/parameter substitution, and no shell
    operators. Returns None (treated as unsafe) otherwise. This is the gate that
    stops a safe-looking prefix (e.g. ``git log``) from smuggling a second command
    (``; rm -rf ~`` or a second line)."""
    # Newlines chain commands but are treated as whitespace by shlex, so the
    # operator check below would never see them — reject multi-line outright.
    if "\n" in command or "\r" in command:
        return None
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

    # gh CLI: positional read allowlist (+ gh api GET/HEAD only)
    if binary == "gh":
        return _is_safe_gh(argv)

    # git: positional read allowlist only
    if binary == "git":
        return _is_safe_git(argv)

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

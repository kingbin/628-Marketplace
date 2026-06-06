---
description: One-time CMC setup — verify dependencies and build ~/Applications/CMC.app
---

Run the CMC one-time setup. This verifies required dependencies (python3, Google
Chrome, curl, lsof, pgrep, osascript), builds `~/Applications/CMC.app`, and seeds
`~/.claude/cmc.json` if it is missing. It is idempotent and safe to re-run.

Run this command now using the Bash tool — no confirmation needed:

```bash
bash "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/cache/628-Marketplace/cmc/0.1.0}/hooks/scripts/cmc-setup.sh"
```

Report the setup result to the user. If any required dependency is missing,
surface exactly which one and stop — the user must resolve it before CMC will
work. On success, tell the user they can now run `/cmc` to open the dashboard.

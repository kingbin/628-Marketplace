---
description: Open or reopen the CMC (Claude Management Console) dashboard Chrome panel
---

Open or reopen the CMC (Claude Management Console) dashboard Chrome panel.

Run this command now using the Bash tool — no confirmation needed:

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d "$HOME"/.claude/plugins/cache/628-Marketplace/cmc/*/ 2>/dev/null | sort -V | tail -1)}"; bash "${ROOT%/}/hooks/scripts/cmc-open.sh"
```

After running, confirm to the user that the CMC dashboard has been opened. If there is an error, report it briefly.

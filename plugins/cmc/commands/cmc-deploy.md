---
description: Deploy the CMC source into the live install and restart the server
---

Deploy the latest CMC plugin source into the live install and force-restart the
server, then report the result. The deploy script is path-agnostic — it resolves
the source and cache locations dynamically, so this works on any machine.

Run this command now using the Bash tool — no confirmation needed:

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d "$HOME"/.claude/plugins/cache/628-Marketplace/cmc/*/ 2>/dev/null | sort -V | tail -1)}"; bash "${ROOT%/}/scripts/cmc-deploy.sh"
```

Report the deploy result. If the server does not come up healthy, surface the
error and the last log lines.

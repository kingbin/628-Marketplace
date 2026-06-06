# 628-Marketplace

A personal [Claude Code](https://claude.com/claude-code) plugin marketplace for
628 Productions. Currently ships one plugin: **CMC**.

## Plugins

### `cmc` — Claude Management Console

A real-time local dashboard that monitors every active Claude Code session,
served at `http://127.0.0.1:7477` and displayed in a dedicated Chrome panel
(`~/Applications/CMC.app`) with its own dock icon and Mission Control space.

**Features**
- Live view of all running Claude Code sessions
- Jump to a session's tmux/iTerm2 pane; open its working dir in Fork / VS Code / PhpStorm
- Active-pane highlighting (the focused tmux pane gets a blue border)
- Optional safe auto-accept for low-risk tool calls
- Started automatically by a `SessionStart` hook

**Requirements**
- macOS
- `python3` (the server is pure standard library — no pip packages)
- Google Chrome at `/Applications/Google Chrome.app`
- Standard CLI tools: `curl`, `lsof`, `pgrep`, `osascript` (all macOS built-ins)

## Install

Add this marketplace and install the plugin:

```bash
claude plugin marketplace add ~/Developer/628Productions/claude/628-Marketplace
claude plugin install cmc@628-Marketplace
```

(Or `claude plugin marketplace add kingbin/628-Marketplace` once pushed to GitHub.)

## First-run setup

After installing, run the one-time setup once:

```
/cmc-setup
```

It verifies dependencies, builds `~/Applications/CMC.app`, and seeds
`~/.claude/cmc.json` from the template if it does not already exist. It is
idempotent — safe to re-run any time (e.g. after Chrome moves).

## Commands

| Command       | What it does                                                        |
|---------------|---------------------------------------------------------------------|
| `/cmc`        | Open or focus the CMC dashboard panel.                              |
| `/cmc-setup`  | One-time setup: verify deps, build `CMC.app`, seed config.          |
| `/cmc-deploy` | Sync this repo's source into the live install and restart the server. |

## Configuration

`~/.claude/cmc.json` (seeded from `plugins/cmc/hooks/scripts/cmc-config.template.json`):

```json
{
  "enabled": true,
  "browser": "chrome",
  "panel_width": 350,
  "safe_auto_accept": false
}
```

Set `"enabled": false` to stop the server and close the panel on the next
`SessionStart`.

## Development & deploy workflow

The canonical source lives here in `plugins/cmc/`. This repo is also the
registered marketplace source, so the source and the registration are the same
directory — there is no separate backing copy to keep in sync.

After editing source, push it into the live install with:

```bash
~/bin/cmc-deploy            # sync repo → live cache, restart server, verify
~/bin/cmc-deploy --dry-run  # preview the rsync changes only
```

(`/cmc-deploy` runs the same script.)

**Why a deploy script is needed** — the `SessionStart` hook only starts the
server if one isn't already running, so reloading the plugin does *not* pick up
new code while a server is alive. Worse, a stale/orphaned `cmc-server.py` can
keep holding port `7477`: the freshly launched copy fails to bind, dies, but the
health check still passes because the orphan answers — so old code keeps serving
silently. `cmc-deploy` kills whatever holds `:7477` (via `lsof`) plus any stray
`cmc-server.py` (via `pgrep`) before starting the cache copy fresh.

Verify the live server is current:

```bash
curl -s http://127.0.0.1:7477/api/sessions   # should contain "focused": true for the active pane
```

### Known caveat

The `/cmc` and `/cmc-setup` commands include a fallback path that pins the
plugin version (`.../628-Marketplace/cmc/0.1.0/...`), used only if
`$CLAUDE_PLUGIN_ROOT` is unset. In normal use `$CLAUDE_PLUGIN_ROOT` is set, but
bump that literal if the plugin version changes.

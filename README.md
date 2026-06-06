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

Add this marketplace and install the plugin. On **any machine** (this is the
portable path — nothing is tied to a specific home directory):

```bash
claude plugin marketplace add kingbin/628-Marketplace   # from GitHub
claude plugin install cmc@628-Marketplace
```

Or, if you have the repo cloned locally and want to develop against it:

```bash
claude plugin marketplace add /path/to/628-Marketplace   # local directory source
claude plugin install cmc@628-Marketplace
```

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
| `/cmc-deploy` | Sync the source into the live install and restart the server (path-agnostic). |

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

After editing source, push it into the live install with either:

```bash
/cmc-deploy        # slash command — works anywhere CMC is installed
cmc-deploy         # terminal wrapper installed by /cmc-setup (--dry-run to preview)
```

Both run `plugins/cmc/scripts/cmc-deploy.sh`, which is **path-agnostic**: it
resolves the source repo from Claude Code's `known_marketplaces.json` (wherever
you cloned it), reads the version from `plugin.json`, and derives the live cache
under `$HOME`. No hardcoded user or version — carry it between machines unchanged.
If there's no editable source (a GitHub-only install), it just restarts the
cached copy in place.

**Why a deploy script is needed** — the `SessionStart` hook only starts the
server if one isn't already running, so reloading the plugin does *not* pick up
new code while a server is alive. Worse, a stale/orphaned `cmc-server.py` can
keep holding port `7477`: the freshly launched copy fails to bind, dies, but the
health check still passes because the orphan answers — so old code keeps serving
silently. The deploy script kills whatever holds `:7477` (via `lsof`) plus any
stray `cmc-server.py` (via `pgrep`) before starting the cache copy fresh.

Verify the live server is current:

```bash
curl -s http://127.0.0.1:7477/api/sessions   # should contain "focused": true for the active pane
```

## Portability

Everything resolves dynamically — there are no machine-specific paths baked into
the plugin:

- Commands resolve their script via `$CLAUDE_PLUGIN_ROOT`, falling back to a glob
  of the latest installed version (no pinned version number).
- The deploy script resolves source/cache from `known_marketplaces.json` + `$HOME`.
- `/cmc-setup` (re)builds `~/Applications/CMC.app` and installs the `~/bin/cmc-deploy`
  terminal wrapper, so a fresh machine is one `/cmc-setup` away from fully set up.

To set up on a new machine: install via the GitHub marketplace (above), run
`/cmc-setup`, then `/cmc`. macOS + Google Chrome are the only external
requirements; the server itself is pure Python standard library.

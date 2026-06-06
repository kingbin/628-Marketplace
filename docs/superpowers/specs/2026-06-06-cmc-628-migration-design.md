# CMC Migration into 628-Marketplace — Design

**Date:** 2026-06-06
**Status:** Approved (proceeding to implementation)

## Goal

Make `628-Marketplace` the owned, in-git canonical home for the CMC (Claude
Management Console) plugin, collapsing the historical three-copy drift. Carry
over the `cmc.md` fix that currently lives only in the live cache, add a
one-time setup command (build `CMC.app`, verify dependencies), write a README,
and push to the personal GitHub remote.

## Decisions (from brainstorming)

- **Migration shape:** 628 IS the marketplace AND the registered source.
- **Marketplace name:** `628-Marketplace` (forces re-register + reinstall;
  install ref becomes `cmc@628-Marketplace`; live cache moves to
  `~/.claude/plugins/cache/628-Marketplace/cmc/0.1.0`).
- **Bundle id:** rebrand the old work-org bundle id → `com.628productions.cmc`.
- **Branch:** `master`, push `-u origin master`.
- **Orphan cleanup:** re-point only. Old `~/Developer/claude/plugins/cmc-plugin`
  dir and the prior work-marketplace `plugins/cmc` source are left on disk, just
  unused. The old
  `cmc-standalone` marketplace *registration* is removed so it stops being used.
- **plugin.json author:** `Chris Blazek`.

## Repo structure

```
628-Marketplace/
├── README.md                          # NEW
├── CMC-HANDOFF.md                     # kept (history)
├── docs/superpowers/specs/...         # this doc
├── .claude-plugin/
│   └── marketplace.json               # NEW — name "628-Marketplace"
└── plugins/cmc/
    ├── .claude-plugin/plugin.json     # author → Chris Blazek
    ├── assets/claude.icns             # NEW — vendored from existing CMC.app
    ├── commands/
    │   ├── cmc.md                     # + carried-over fix ($HOME, new cache path)
    │   ├── cmc-deploy.md
    │   └── cmc-setup.md               # NEW
    └── hooks/
        ├── hooks.json
        └── scripts/
            ├── claude-monitor.sh
            ├── cmc-config.template.json
            ├── cmc-open.sh            # CMC_BUNDLE_ID rebranded
            ├── cmc-server.py
            ├── cmc-setup.sh           # NEW
            ├── safe-auto-accept.py
            └── startup.sh
```

Source is copied from the prior canonical source (byte-identical to the live cache
except `cmc.md`), then the fix re-applied.

## Components

### marketplace.json
`name: "628-Marketplace"`, owner `Chris Blazek`, one plugin entry `cmc` →
`./plugins/cmc`, category `productivity`.

### /cmc-setup (cmc-setup.md → cmc-setup.sh)
Idempotent one-time setup:
1. **Verify deps**, ✓/✗ each: `python3`, `/Applications/Google Chrome.app`,
   `curl`, `lsof`, `pgrep`, `osascript`. Server is stdlib-only — no pip step.
2. **Build `~/Applications/CMC.app`**: write `Info.plist` (bundle id
   `com.628productions.cmc`), the `MacOS/CMC` Chrome `--app` launcher, copy
   `assets/claude.icns` → `Resources/`, `chmod +x`. Rebuild cleanly if present.
3. **Seed `~/.claude/cmc.json`** from `cmc-config.template.json` if missing.

### cmc.md fix (de-hardcoded)
```bash
bash "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/cache/628-Marketplace/cmc/0.1.0}/hooks/scripts/cmc-open.sh"
```
`$HOME` instead of a hardcoded user; cache path updated to the new marketplace.
Version pin in the fallback remains (safety net only) — noted as a caveat.

### ~/bin/cmc-deploy re-point
- `SRC` → `~/Developer/628Productions/claude/628-Marketplace/plugins/cmc`
- `CACHE_PARENT` → `~/.claude/plugins/cache/628-Marketplace/cmc`
- Remove the `REG` (registered-dir) sync — source and registration are now the
  same dir.
- Header comment: source freshness now comes from `git pull` in 628, not
  `prdev --pull`.

### README.md
What 628-Marketplace is; the CMC plugin; requirements; install + register;
`/cmc-setup` first run; the three commands (`/cmc`, `/cmc-setup`, `/cmc-deploy`);
deploy workflow + the port-7477 stale-orphan gotcha.

## Registration steps (live state)
1. `claude plugin marketplace add ~/Developer/628Productions/claude/628-Marketplace`
2. `claude plugin install cmc@628-Marketplace`
3. Retire old: uninstall `cmc@cmc-standalone`, remove `cmc-standalone` marketplace.
4. Run setup (`cmc-setup.sh`), verify server health + `CMC.app`.

Verify: `claude plugin marketplace list` shows `628-Marketplace`;
`curl -s http://127.0.0.1:7477/api/sessions` contains `"focused"`.

## Out of scope
Old `~/Developer/claude/plugins/cmc-plugin` dir and the prior work-marketplace
`plugins/cmc` source are left on disk (flagged in the final summary). No changes
to the prior work marketplace.

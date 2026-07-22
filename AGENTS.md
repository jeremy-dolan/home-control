# Agent guide

Operating guidance for coding agents in this repo. Read alongside
`ARCHITECTURE.md` (how the code is built).

## Repo rules

- Never force-push, merge, or push to `main` without the user explicitly asking.
- `dev/`, `standalone-apps/`, `plugins/`, `README.ideas` are gitignored on
  purpose (see the directory map in `ARCHITECTURE.md`) — don't `git add -f`
  them without checking with the user first.
- `.claude/` is partly tracked: `.claude/skills/` and `.claude/settings.json`
  are committed; everything else under it is ignored — `settings.local.json`
  is per-machine permission grants, `worktrees/` is agent scratch space. Keep
  skills checkout-agnostic (derive paths from git, don't hardcode
  `/home/<user>/...`) so they work from a worktree.

## Code conventions

- The codebase runs a deliberately low comment density. Reserve inline
  comments for non-obvious behavior of code that is *present*.
- **Don't comment on code that isn't there** — put the reason something was
  removed in the commit message, not a note at the site.

## Keeping docs current

If you change the `System` contract, directory layout, config handling, or
common commands, update `ARCHITECTURE.md` in the same commit.

If you change how a device is discovered or configured, update both places the
per-device blurbs live — `README.md`'s `## Device support` and that panel's
`help_notes()` (see Config in `ARCHITECTURE.md`).

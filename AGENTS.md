# Agent guide

Operating guidance for coding agents in this repo. Read alongside
`ARCHITECTURE.md` (how the code is built).

## Repo rules

- Never force-push, merge, or push to `main` without the user explicitly asking.
- Don't `git add -f` an ignored path without checking with the user first.
- Skills under `.claude/skills/` are tracked; the rest of `.claude/` is
  machine-local scratch. Editing a skill is a repo change — commit it. Skills
  also run from worktrees, so derive paths with `git rev-parse` instead of
  hardcoding a checkout (see `verify`).

## Code conventions

- The codebase runs a deliberately low comment density. Reserve inline
  comments for non-obvious behavior of code that is *present*.
- **Don't comment on code that isn't there** — put the reason something was
  removed in the commit message, not a note at the site.

## Commit messages

Follow the 50/72 rule: subject of 50 characters or fewer, blank line, body
hard-wrapped at 72. Subject in the imperative mood, no trailing period.
`.githooks/commit-msg` enforces all but the mood.

Prefix the subject with the subsystem it touches — `midea:`, `ui:`, `tools:`,
`docs:` — and drop the prefix only when the change is genuinely repo-wide.

## Keeping docs current

If you change the `System` contract, directory layout, config handling, or
common commands, update `ARCHITECTURE.md` in the same commit.

If you change how a device is discovered or configured, update both places the
per-device blurbs live — `README.md`'s `## Device support` and that panel's
`help_notes()` (see Config in `ARCHITECTURE.md`).

When a work session looks finished — a PR merged, the branch's task delivered,
or the user signals they're wrapping up — suggest running `/wrap-up` once. It
checks whether anything learned this session belongs in these docs or memory,
and whether the branch settled any `TODO.md` entry (usually nothing on both
counts). Offer it; don't run it unprompted.

## Adding support for a new device

Adding a device integration, or repointing an existing panel at different
hardware? Invoke the `add-device` skill — it carries the full recipe (library
research, Controller/Panel implementation, config, registration, mocks, docs).

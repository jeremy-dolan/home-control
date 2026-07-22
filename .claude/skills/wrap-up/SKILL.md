---
name: wrap-up
description: End-of-session check on whether anything learned this session belongs in ARCHITECTURE.md, AGENTS.md, TODO.md, or memory. High bar — the default and most common outcome is "nothing to do".
---

# Session wrap-up

Run at the end of a work session to decide whether anything from **this
session** belongs in the project's long-lived docs (`ARCHITECTURE.md`,
`AGENTS.md`, `TODO.md`) or your persistent memory.

**The default outcome is "nothing to do." Say so and stop.** A quiet session is
a normal session. Do not manufacture updates to look productive — a needless
entry in an always-loaded file is a cost, not a contribution. Most sessions
change code, and code + git history already record that; they need no doc edit.
`TODO.md` is not always-loaded, but the bar for *adding* to it is the same one
in step 2; the only thing it is cheap to do there is strike work this branch
actually finished.

## 1. Gather what actually changed

Review the session concretely, not from memory: `git diff main...HEAD` and the
branch's commits, decisions the user made, gotchas hit, and any environment or
account facts learned. Build a short list of *candidate* facts.

Then read `TODO.md` once, looking **only** for entries the branch's own work
touched — items it finished, partly finished, or made moot. This is a targeted
check against the diff you just read, not an audit of the backlog: TODO entries
the session never went near are none of this skill's business.

## 2. Keep only what clears the bar

A candidate qualifies **only** if it is one of:

- **Stale** — the work made an existing statement in `ARCHITECTURE.md`,
  `AGENTS.md`, a memory, or `TODO.md` *wrong* (e.g. a `System` contract method
  renamed, a file moved, config handling changed, a documented gotcha now
  fixed, a TODO item this branch just delivered or designed away). This is the
  highest-priority trigger: a wrong doc is worse than a missing one, and a
  backlog listing finished work is the easiest kind of wrong to leave behind.
- **New, durable, and important** — the session established something future
  sessions will need that is **not** recoverable from the code or git history,
  and is not already written down (e.g. work the user explicitly deferred —
  "not now, later" — which git records nowhere).

Reject everything else, including: routine feature work (git has it),
implementation detail that's readable straight from the code, facts that only
mattered for this one task, and half-formed ideas.

## 3. Route each survivor to its right home — usually NOT these files

`ARCHITECTURE.md` and `AGENTS.md` load into every session, so the bar for
adding to them is high and additions must be terse and broadly applicable.
Before editing either, check whether the fact belongs somewhere cheaper:

- **Non-obvious behavior of code that exists** → an inline comment at the site,
  not a doc.
- **Future work / design notes / research, and striking or amending entries the
  session settled** → `TODO.md`.
- **User-facing device behavior** → `README.md`'s `## Device support` (keep in
  sync with the panel's `help_notes()`).
- **How the code is structured** (System contract, layout, config handling,
  commands, a genuinely new architectural concept) → `ARCHITECTURE.md`.
- **How an agent should operate here** (a workflow rule, the add-a-device
  recipe) → `AGENTS.md`.
- **Anything that can't go in a public repo** — the maintainer's personal
  preferences, environment/sandbox specifics, private IPs, tokens, credentials
  → **memory** (one fact per file; update the `MEMORY.md` index; prefer
  updating an existing memory over adding a duplicate; delete any memory this
  session proved wrong).

## 4. Propose — don't apply

Present the specific edits (file + concise before/after) and let the user
approve before writing anything. Never auto-commit; follow the maintainer's
git workflow. When memory changes, update the `MEMORY.md` index in the same
step. If nothing cleared the bar, report that in one line and stop.

# Changelog

Notable changes, newest first. Follows [Keep a Changelog](https://keepachangelog.com)
and [semantic versioning](https://semver.org).

## [Unreleased]

### Fixed

- **`arbiter_judges` no longer passes a card when it cannot reach a model.**
  A crash, a timeout or an empty reply returned `pass`, which made the only
  gate that costs money the only gate a network blip could walk straight
  through. It now separates the reasons: a transient failure defers and
  retries three times, an unreadable reply or a missing arbiter escalates.
  Deferring is bounded because an unbounded retry on a condition that never
  changes is a silent stall.
- **Arbiter spend is recorded rather than discarded.** `total_cost_usd` was
  parsed off every call and thrown away with the envelope, so judgments,
  adjudications and triage were invisible to the board total, to subtree
  budgets and to `budget_remaining`. Agent runs and arbiter calls are counted
  separately — a run still means an agent worked a card.
- **The arbiter's JSON extractor no longer counts braces.** One unbalanced
  brace inside a string ended the object early, and since an unreadable reply
  passed, a model writing `"missing } in config"` sent the card through the
  acceptance gate. It uses the stdlib decoder now.
- **`PUT /api/config` merges instead of replacing sections.** A body naming
  one key inside a section wrote only that key to disk. It hid well: the
  defaults are merged back in on read, so only settings someone had
  deliberately changed were lost, silently, back to default.

### Changed

- SECURITY.md enumerates what the board can do without authenticating, rather
  than only noting that it has no login.

## [0.1.0] — 2026-08-28

First public release.

### The idea

A deterministic scheduler owns the loop and never forms an opinion about whether
the work is finished; a model is called for judgment. A subroutine cannot halt
the loop by returning, which is why an agent session stops at every milestone
and this does not.

### Added

- **Board and scheduler.** Cards, three edge kinds (ordering, artifact, mutex),
  a ready set, leases with crash recovery, and an append-only event log.
- **Card types own pipelines.** Each stage names the agent that works it and the
  gates it must clear. Editable in the web board, portable between repos.
- **Gates with four verdicts** — `pass`, `defer` (requeue, no attempt spent),
  `fail` (return with evidence, which becomes the next attempt's brief), and
  `escalate`. A gate is an executable: the card on stdin, a verdict on stdout.
- **Checkpoints** that hold their own subtree rather than the board, carry the
  diff and evidence with them, and can auto-pass trivia or expire on an SLA.
- **Proposals.** Agents never write to the board; new work is adjudicated by a
  policy tier, then a model, then a human. Five invariants hold at every tier.
- **Containment** — subtree and board-wide budgets, fan-out and depth caps, a
  dead-letter column, and an expansion alarm that counts agent-created cards.
- **Merging.** A finished card is rebased, has its completion gates re-run on
  the rebased tree, and is fast-forwarded. Nothing is ever forced, and `done` is
  verified against the branch rather than assumed.
- **Direction** (`dispatch intent`). Describe what you want; a planner reads the
  repo and proposes cards with risks and out-of-scope; you approve the plan.
- **Shared memory** with SQLite FTS5, injected into prompts rather than looked
  up, so agents do not start cold.
- **OS-level confinement**, on by default where the OS supports it: Seatbelt on
  macOS, bubblewrap on Linux, leaving the network alone. `srt` optionally
  restricts egress too.
- **Session integration** — `dispatch attend` blocks until a decision is the
  session's; `dispatch channel` is a Claude Code channel that pushes a pointer
  into a running session; `dispatch wait` blocks on one card.
- **A local web board** with live updates, a pipeline editor, a Needs You
  column, and a "why nothing's running" panel.
- **Model selection per card and per workflow stage.** Three places can say
  which model works a card and the most specific wins: the card (`dispatch add
  --model opus`), then the stage, then the agent role in `agents.json`.
  `dispatch show` names which of the three decided, and `runs.model` records
  what actually ran so mixed-model spend stays attributable.
- **A manual that ships with the tool** — `dispatch docs`, also exported to
  `docs/`.

### Notes

- Python 3.9+, standard library only. No runtime dependencies.
- Agents bill your claude.ai subscription by default, because an
  `ANTHROPIC_API_KEY` in the environment silently outranks it.
- Linux confinement is exercised in CI; macOS has additionally been used in
  anger.

[Unreleased]: https://github.com/GallagherSam/dispatch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GallagherSam/dispatch/releases/tag/v0.1.0

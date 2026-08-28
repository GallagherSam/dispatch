# dispatch

A kanban board that drives a fleet of coding agents.

The premise: **the loop is code, the model is a subroutine.** A Claude Code
session stops at every milestone because the session *is* the loop, and an LLM
loop exits the moment the model emits plain text. Here a deterministic scheduler
owns the loop and never forms an opinion about whether the work is finished — it
asks one question, *is the ready set empty?*, and dispatches if it isn't. The
model is called for judgment, and a subroutine cannot halt the loop by returning.

Python 3.9+, standard library only. No services, no daemon but its own, no
dependencies.

---

## Install

```bash
pipx install git+https://github.com/GallagherSam/dispatch
dispatch docs                 # the manual, also readable in the terminal
```

You will also need [Claude Code](https://claude.com/claude-code) on your PATH —
dispatch drives it. The runner command is a template, so another agent CLI could
slot in, but none has been tried.

## A worked example

```bash
cd ~/code/your-repo
dispatch init
```

`init` scaffolds `.dispatch/`, detects your test command and **runs it** before
storing it — a command that does not work makes every completion gate
meaningless while the board looks healthy. It confines agents to their own git
worktree wherever the OS supports it, and picks a stable port for this repo.

Describe what you want, rather than decomposing it yourself:

```bash
dispatch intent "Rate limiting on the public API. Per API key, not per IP —
we have customers behind shared NAT. Existing endpoints must keep their
latency budget."
```

A planner reads the repo and proposes cards — brief, acceptance criteria,
scope, dependencies — plus the risks and what it deliberately left out. Approve
the plan and the cards are created with their edges:

```bash
dispatch plan t_abc123          # read the proposal
dispatch respond c_xy12 approve # build it
dispatch up -d                  # scheduler + board at 127.0.0.1:7837
```

Each card moves through the pipeline its card type defines, one agent per
stage, gated at every boundary:

```
build ─────► qa ─────► review ─────► signoff ─────► integrate ─────► merged
developer    qa        reviewer      you            integrator
```

Nothing declares itself finished. A gate does, and gates have four verdicts —
`pass`, `defer` (requeue, **no attempt spent**), `fail` (return it with the
evidence, which becomes the next attempt's brief), and `escalate`. A finished
card is rebased, re-verified on the rebased tree, and fast-forwarded onto your
base branch.

When something needs judgement, the session that seeded the cards can answer it
instead of you:

```bash
dispatch attend                              # blocks until a decision is yours
dispatch respond c_ab12 approve --as session
```

Money, secrets and runaway detection stay your call — a session that tries to
answer one is refused.

```bash
dispatch status      # one screen: scheduler, spend, sandbox, what is in flight
dispatch needs       # decisions waiting on you
dispatch blocked     # for every unfinished card, exactly what holds it
```

## What it enforces, rather than hopes for

- An agent works in an isolated worktree and may only touch the globs its card
  declares. Confinement is at the OS level; the diff check is the second net.
- An agent cannot mark its own card done, edit its own gates or acceptance
  criteria, create a cycle, or exceed its subtree's budget.
- New work arrives as a *proposal* and is adjudicated. Agents never write to the
  board.
- A card nobody can check is refused **before** it costs a run.
- A card that says `done` is verified against its branch, because work stranded
  on a branch nobody looks at again is the quietest failure this can produce.

## Documentation

[`docs/`](docs/) — the same pages `dispatch docs <topic>` prints in the
terminal, which is what agents read. Start with
[overview](docs/overview.md) and [setup](docs/setup.md).

| | |
|---|---|
| [cards](docs/cards.md) · [workflows](docs/workflows.md) · [gates](docs/gates.md) | writing work an agent can finish and a gate can judge |
| [direction](docs/direction.md) · [proposals](docs/proposals.md) | describing goals, and how agents ask for more |
| [checkpoints](docs/checkpoints.md) · [sessions](docs/sessions.md) · [channels](docs/channels.md) | decisions, and who answers them |
| [merging](docs/merging.md) · [sandbox](docs/sandbox.md) · [memory](docs/memory.md) | landing work, confining agents, and not starting cold |
| [serving](docs/serving.md) · [billing](docs/billing.md) · [config](docs/config.md) · [cli](docs/cli.md) | running it |
| [troubleshooting](docs/troubleshooting.md) | when something is stuck |

## Tests

```bash
python3 -m unittest discover -s tests -t .     # 535 tests, ~65s, no model calls
```

The suite drives the real scheduler, real gates and real git worktrees against a
throwaway repo; only the model call is replaced. Every bug found in use has a
named regression test — look for `# REGRESSION:`.

## Known limits

- Both sandbox backends are exercised in CI by actually trying to escape them,
  but only macOS Seatbelt has been used in anger. If you are on Linux and
  confinement matters to you, verify it yourself before trusting it.
- The board has no login of its own. It refuses cross-origin writes and binds to
  loopback by default; on a tailnet, your ACLs are the boundary.
- Channels are a Claude Code research preview, so a custom one needs a
  development flag. `dispatch attend` needs none.

## Contributing, and the rest

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) ·
[CHANGELOG.md](CHANGELOG.md) · [Apache-2.0](LICENSE)

**dispatch runs AI agents unattended against your repository.** They write files
and execute commands. Read [SECURITY.md](SECURITY.md) before pointing it at
anything you care about — in particular, gate scripts live in `.dispatch/` and
are meant to be committed, so running dispatch on a repository you did not write
executes that repository's scripts.

**The sharpest risk in daily use is acceptance-criteria quality.** A gate can only be as good
as the check behind it, and "the feature works" is not executable. If briefs
keep arriving without a runnable check, put `spec` first in the pipeline and let
an agent write them.

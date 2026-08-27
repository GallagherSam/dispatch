# Overview
> What dispatch is and the one idea it is built around.

A kanban board that drives a fleet of coding agents.

**The loop is code; the model is a subroutine.** An agent session stops at every
milestone because the session *is* the loop, and an LLM loop exits the moment the
model emits plain text. In dispatch a deterministic scheduler owns the loop. It
never forms an opinion about whether the work is finished — it asks one question,
*is the ready set empty?*, and dispatches if it isn't. A model is called for
judgment, and a subroutine cannot halt the loop by returning.

## Three parts

| part | role |
|---|---|
| **board** | durable state: cards, edges, gates, budgets, an append-only event log |
| **scheduler** | deterministic control: ready set, gates, leases, retries, merges |
| **arbiter** | judgment on demand: a stateless model call for one decision |

## The cycle

    card -> stage gates (pre_dispatch) -> agent runs in its own git worktree
         -> stage gates (pre_complete) -> next stage -> ... -> merge to base

A card moves right through the stages its **card type** defines. Each stage names
the agent that works it and the gates it must clear. When the pipeline is
exhausted the card is rebased, re-checked, and fast-forwarded onto the base
branch.

## What is enforced, not hoped for

- An agent works in an isolated worktree and may only touch the globs its card
  declares (`diff_scope`).
- An agent cannot mark its own card done. Only a gate can.
- An agent cannot modify its own gates or acceptance criteria.
- New work arrives as a *proposal* and is adjudicated. Agents never write to
  the board.
- A parent's budget is the ceiling for everything beneath it.

## Where things live

    .dispatch/
      board.db          sqlite: cards, edges, gates, runs, events
      config.json       stages, concurrency, containment, runner
      workflows.json    card types -> pipelines (commit this)
      settings.json     what agents may run inside their worktree
      agents.json       model and tool allowlist per role
      agents/           one prompt file per role
      gates/            executable gates
      runs/             per-run prompt, stdout, diff, result
      worktrees/        one git worktree per card

Next: `dispatch docs setup`

# Proposals
> How a working agent tells the board about work it should not do itself.

Agents never write to the board. Direct writes would let an agent unblock
itself, redefine its own acceptance criteria, or fan out without limit. Every
change from a worker arrives as a proposal and is adjudicated.

## From inside a worktree

    dispatch propose --from $DISPATCH_TASK_ID --kind add_task \
      --title "Session store needed for auth middleware" \
      --brief "The middleware assumes a session store that does not exist yet." \
      --rationale "out of scope for this card"

| kind | use |
|---|---|
| `add_task` | adjacent work you found; lands as a *sibling* |
| `split` | this card is too big; the pieces become children and block it |
| `add_dep` | `--src A --dst B`: B must wait for A |
| `amend_brief` | `--task T --append "..."` on another card |
| `raise_blocker` | `--reason "..."`: you are stuck, park this card |
| `request_gate` | this card needs a check it does not have |
| `escalate` | a human has to decide; use `--urgency high` |

Rule of thumb: if you find work that belongs on the board but not in this card,
propose it rather than doing it. Scope creep inside a card is invisible to the
board and gets rejected by `diff_scope` anyway.

## The ladder

1. **policy** — deterministic. Same parent, in budget, in depth, no cycle →
   accepted. No model, no cost. This is where most proposals end.
2. **arbiter** — one stateless model call with the relevant board slice.
   `split`, `cancel` and `request_gate` go here by default.
3. **human** — a checkpoint in *Needs You*.

Set the default with `mutation.autonomy`: `policy` | `arbiter` | `human`.

## Invariants, at every tier

- no cycles, ever
- an agent may not modify its own gates or acceptance criteria
- an agent may not mark its own card done — only a gate can
- a proposal cannot exceed its parent subtree's remaining budget
- near-duplicates merge into the existing card rather than adding one

## Watching them

    dispatch proposals          # what was proposed and how each was decided

Every decision is on the event log with its reasoning, including the arbiter's.

## When the board grows faster than it shrinks

These systems do not stall, they proliferate. `containment` caps children per
parent and decomposition depth, and an expansion alarm watches created ÷
completed over a rolling window — above the limit, dispatch pauses and opens a
checkpoint. That is the "we are going in circles" detector.

Next: `dispatch docs merging`

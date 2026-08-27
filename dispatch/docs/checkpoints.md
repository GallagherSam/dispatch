# Checkpoints
> Human gates. They hold their own subtree, never the whole board.

A stage whose agent is `human` becomes a checkpoint. So does any gate that
escalates. Everything not downstream of it keeps running — the board idles only
when every live branch is behind one.

## Two kinds, and approval means different things

| kind | opened by | approving means |
|---|---|---|
| `signoff` | a `human` stage | the work is good — advance the card |
| `escalation` | a gate, the adjudicator, a failed merge | let the card run the stage it never reached |

## Answering

    dispatch needs
    dispatch respond c_ab12 approve
    dispatch respond c_ab12 amend  --note "Also handle the empty case."
    dispatch respond c_ab12 reject --note "The retry backoff needs jitter."

- **approve** — advance, or unblock and run
- **amend** — your note is appended to the brief and the card runs again
- **reject** — on a signoff, back a stage with your reason as the brief; on an
  escalation, park the card with your reason

A rejection reason becomes the next agent's instruction, so write it as one.
Rejecting with no note is refused.

## Each checkpoint carries its own context

The diff, the changed files, the agent's summary, the gate verdicts, the
acceptance criteria. The real cost of a human gate is the ten minutes spent
reconstructing what you are being asked; a checkpoint that arrives without
context is a checkpoint people stop answering.

## Keeping trivia away from you

```jsonc
{"stage": "signoff", "agent": "human", "auto_pass_if": "small_and_green"}
```

`small_and_green` passes without asking when the tests are green, under 20 lines
were added, and at most 3 files changed.

## Not answering

```jsonc
{"stage": "signoff", "agent": "human", "sla": "4h", "on_sla": "block"}
```

`sla` takes seconds or `30m` / `4h` / `2d`. When it elapses:

- `block` (default) — park the subtree cleanly with a reason
- `approve` — carry on without you
- `reject` — send it back a stage

An unanswered checkpoint at 3am should park, not hang.

Next: `dispatch docs proposals`

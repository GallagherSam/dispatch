# Troubleshooting
> Nothing is running, or everything is failing.

## Start here

    dispatch status      # scheduler alive? how many in flight? what's it spent?
    dispatch blocked     # for every unfinished card, exactly what holds it

`blocked` is the answer to "why is nothing happening" almost every time. The
scheduler computes it every tick anyway.

## Common holds

| what `blocked` says | what to do |
|---|---|
| `in backlog — not yet started` | `dispatch start t_abc123` |
| `waits on t_xyz` | that dependency is not done |
| `deferred Ns: quota_above ...` | a gate said not yet; it will retry |
| `deferred Ns: merge: ...` | the repo is dirty or on another branch |
| `awaiting your sign-off` | `dispatch needs` |
| `WIP limit on build` | raise it in `config.json`, or let it drain |
| `lock 'integration' held by ...` | integration is serialised on purpose |
| `stage 'x' is not in the y pipeline` | `dispatch workflows` — fix the pipeline |
| `would complete, but N child cards are unfinished` | expected; parents wait |

## The scheduler is not running

    dispatch status              # says "down"?
    dispatch up -d
    tail -f .dispatch/scheduler.log

Run it in the foreground to watch a single pass:

    dispatch tick -n 1 --wait 120

## A card keeps failing

    dispatch show t_abc123

The `last returned because` section is verbatim what the gate told the agent.
If the same gate fails three times, the brief is usually the problem, not the
agent — the acceptance criteria and the brief disagree, or the scope is too
narrow to do the work.

After `max_attempts` a card is dead-lettered rather than retried forever, and
opens a checkpoint. One poison card must not eat a night's quota.

## Agents cannot run the tests

They are writing blind and the gate is the first thing seeing the truth. Add the
command to `.dispatch/settings.json` under `permissions.allow`, or set
`runner.permission_mode` to `bypassPermissions` — they are confined to a
throwaway worktree either way.

## A card says `done` but its work is not on master

See `dispatch docs merging` — it is checked for, and recovers itself.

## A sandboxed card fails where an unsandboxed one does not

Check the backend first: `dispatch status` says which one is in use. Under
`srt`, network is allow-only — `WebFetch` returns `EGRESS_BLOCKED` and a
blocked domain looks like a hung command, so either add it to
`sandbox.allowed_domains` or switch `sandbox.backend` to `auto`. Under
`seatbelt`/`bwrap` the network is untouched, so suspect the writable paths
instead. See `dispatch docs sandbox`.

## "Credit balance is too low" on a subscription

An `ANTHROPIC_API_KEY` in the environment outranks your claude.ai login. Check
`dispatch status` — the `billing` line says which credentials agents will use —
and set `runner.auth` to `subscription`. See `dispatch docs billing`.

## An agent says a web tool "is not granted"

`WebSearch` and `WebFetch` have to be on the role's `allowed_tools` in
`.dispatch/agents.json` *and* in `.dispatch/settings.json`. Both are there by
default; a board initialised by an older version may not have them, and an
agent denied a research tool tends to answer from stale memory instead.

## `status` says paused — is it stuck or was that deliberate?

It says which, and why:

    ◐ scheduler paused (pid 4821)
      paused    expansion alarm — 14 agent-created card(s) per 5 completed

Answer it in `dispatch needs`, or `dispatch resume`. Answering clears the alarm
and restarts the ratio window from now — an acknowledged alarm used to re-fire
forever, and the only way through was to disable the guard.

## The board is growing, nothing ships

The expansion alarm should have paused dispatch and opened a checkpoint. If it
has not, check `containment.expansion_ratio_limit`. Then look at what is being
proposed: `dispatch proposals`. Usually one card's brief is too vague and every
agent that touches it discovers "more work".

## Everything is on fire

    dispatch down
    dispatch status
    # then, once you know what happened:
    dispatch up -d

Pausing from the board (or `scheduler.paused` in config) stops dispatch and
merges without killing the process.

## Reading the history

    dispatch log --limit 200
    dispatch proposals

The event log is append-only and records every state change with its actor, so
"what did the arbiter see when it approved that?" is answerable.

## "tests failed" over evidence that is all passes

A *killed* run, not a failing one — usually credentials expiring mid-suite. The
gate says so at the top of the evidence; run the command yourself before
treating the branch as broken. On `401` or `OAuth token revoked`, logging in
again is not enough: the daemon hands workers the environment it started with,
so `dispatch down && dispatch up`.

## A card sits in `merging`

`ls` and `status` name the reason under the card; `blocked` shows it for every
unfinished card.


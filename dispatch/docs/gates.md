# Gates
> The only thing permitted to declare work complete. Four verdicts, not a boolean.

| verdict | meaning | effect |
|---|---|---|
| `pass` | proceed | dispatch, or advance to the next stage |
| `defer` | not yet, but it will be | requeue with backoff — **no attempt spent** |
| `fail` | the work is wrong | return to the agent with evidence, `attempt++` |
| `escalate` | policy or budget breach | open a human checkpoint |

`defer` versus `fail` is the distinction most systems collapse. Separating them
is what lets a quota gate hold a card for six hours without poisoning it.

When several gates disagree the most restrictive wins:
`escalate > fail > defer > pass`.

## When they run

| hook | question |
|---|---|
| `pre_dispatch` | may this card start right now? |
| `pre_complete` | is the work acceptable? |
| `pre_transition` | may it move from this stage to the next? |
| `post_complete` | what happens downstream? |

## Built in

`tests_pass` `lint_clean` `build_ok` — run the configured command in the worktree
`has_acceptance` — refuses a card nobody can check (pre_dispatch, escalates)
`has_plan` — a proposed plan is reviewable and its cards are workable
`diff_scope` — the diff stayed inside the card's declared globs
`no_secrets` — no keys or credentials in added lines
`no_stray_writes` — the agent stayed inside its worktree (see below)
`arbiter_judges` — a model judges prose acceptance criteria (costs money)
`concurrency` `wip_limit` `mutex_free` `budget_remaining` `time_window`
`quota_above` — reads `.dispatch/gates/quota.sh` for remaining percent

Write them shorthand in a pipeline: `"tests_pass"`, `"quota_above:30"`,
`"wip_limit:build,4"`. Or as an object to force a hook:
`{"gate": "tests_pass", "hook": "pre_transition"}`.

## Writing your own

A gate is an executable in `.dispatch/gates/`. The card arrives as JSON on
stdin; a verdict goes to stdout. Exiting 0 with no output is a pass, so a
one-liner is a valid gate.

```bash
#!/usr/bin/env bash
# .dispatch/gates/changelog.sh
cat > /dev/null
[ -f "$DISPATCH_WORKTREE/CHANGELOG.md" ] \
  && echo '{"verdict":"pass"}' \
  || echo '{"verdict":"fail","reason":"no CHANGELOG.md",
            "evidence":"Add an entry describing this change."}'
```

Environment: `DISPATCH_ROOT` `DISPATCH_TASK_ID` `DISPATCH_STAGE` `DISPATCH_HOOK`
`DISPATCH_WORKTREE` `DISPATCH_DIFF_FILE` `DISPATCH_BOARD_DB`.

Reference it by filename without the extension: `"gates": ["changelog"]`.

## `no_stray_writes`, and why it exists

An agent can resolve an absolute path and write into the main repository rather
than its worktree. That work never reaches the card's branch, so the card
merges nothing — and the dirtied base tree blocks **every** card from merging.
Both ends are silent: the agent reports success, and the board sits in
`merging`.

This gate compares the main repo's dirty paths before and after each run and
returns the card with the list. It is a global `pre_complete` gate, so it is on
whatever your pipeline says.

The real fix is the sandbox, which makes the write fail in the kernel — see
`dispatch docs sandbox`. This gate is what catches it when confinement is off.

## Evidence is the retry's instruction

Whatever a failing gate puts in `evidence` is handed to the next attempt as its
brief. Write it as an instruction, not a complaint: "Add a CHANGELOG.md entry"
beats "missing changelog".

## The quota gate

`.dispatch/gates/quota.sh` should print remaining percent on stdout. Until you
teach it, it exits non-zero and the gate passes — a gate you have not taught must
not silently stop the board.

Next: `dispatch docs checkpoints`

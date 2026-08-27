# Cards
> Writing a card an agent can actually finish and a gate can actually judge.

A card is one shippable change. It carries a brief (the literal prompt an agent
receives), acceptance criteria (what it is judged against), and a scope (what it
may touch).

## Acceptance criteria are the whole game

A gate can only be as good as the check behind it. "The feature works" is not an
acceptance criterion; `pytest tests/test_auth.py::test_session_expiry` is.

**At least one criterion should be a runnable command.** `has_acceptance` runs
*before* dispatch and escalates to a human, so a card nobody can check costs one
minute of your attention instead of three agent runs that learn nothing.

If a request is too vague to write a check for, make it a `research` card whose
output is the spec, and let the next card implement it.

## Scope

    --scope "src/api/**" --scope "tests/**"

The `diff_scope` gate rejects a diff that strays. This is how four agents work in
parallel without stepping on each other — a checked invariant, not a hope. `*`
stops at a `/`; `**` does not.

## A good card

    dispatch add "Retry failed webhook deliveries" \
      --brief "src/webhooks/deliver.py sends once and drops on failure.
    Add exponential backoff with jitter, 5 attempts, dead-letter to the
    webhook_failures table after that. Do not change the delivery payload." \
      --accept "pytest tests/test_webhooks.py passes" \
      --accept "a delivery that fails 5 times lands in webhook_failures" \
      --scope "src/webhooks/**" --scope "tests/**" \
      --tag webhooks --start

Note what the brief does: names the file, says what is wrong, says what to build,
and says what to leave alone. Agents follow the last one as much as the others.

## Fields worth knowing

| flag | meaning |
|---|---|
| `--type` | card type, which picks the pipeline (`dispatch workflows`) |
| `--parent` | containment and a shared budget ceiling, not ordering |
| `--depends-on` | ordering |
| `--budget` | usd ceiling for this card and everything beneath it |
| `--priority` | higher goes first out of the ready set (default 50) |
| `--max-attempts` | returns before dead-lettering (default 3) |
| `--start` | put it straight on stage 1 instead of the backlog |

## Parent is not a dependency

A parent contains: it rolls up, it caps the subtree's budget, cancelling it
cancels the children, and it will not complete while a child is unfinished. A
dependency is ordering and nothing else. Use both, for different things.

## Editing

    dispatch show t_abc123
    dispatch edit t_abc123 --accept "pytest -q passes" --scope "src/**"
    dispatch edit t_abc123 --append-brief "Also handle the empty case."
    dispatch edit t_abc123 --requeue      # clear a block and try again

Next: `dispatch docs workflows`

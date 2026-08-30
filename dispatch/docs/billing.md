# Billing
> Which credentials the agents use, and what the spend figures mean.

Agents run `claude` as a subprocess, so they use whatever credentials that
subprocess finds. **An `ANTHROPIC_API_KEY` in your environment silently
outranks your claude.ai login**, which is how a fleet of agents can burn API
credits all night while you believe it is on your subscription.

## The default is your subscription

`runner.auth` is `subscription`, which removes every API auth variable from the
agent's environment so the claude.ai login is used:

    ANTHROPIC_API_KEY        ANTHROPIC_AUTH_TOKEN     ANTHROPIC_BASE_URL
    ANTHROPIC_CUSTOM_HEADERS CLAUDE_CODE_USE_BEDROCK  CLAUDE_CODE_USE_VERTEX
    AWS_BEARER_TOKEN_BEDROCK

Only the agent subprocess is affected; your own shell is untouched.

`dispatch init` and `dispatch status` both say which credentials will be used,
and tell you when a key is being removed:

    billing   claude.ai subscription — ANTHROPIC_API_KEY is set and will be
              removed from the agent environment so it does not bill API credits

## The other modes

```jsonc
"runner": { "auth": "subscription" }   // subscription | api_key | inherit
```

- `api_key` — leave the key in place and bill the API. For CI, or a machine
  with no interactive login.
- `inherit` — pass the environment through untouched. Use this for Bedrock,
  Vertex, or a gateway, where the variables above are the point.

    dispatch init --auth api_key

If you are on a subscription and see *"Credit balance is too low"*, an API key
is in the environment and `auth` is not `subscription`.

## What the money figures mean

`dispatch status` and the board report `total_cost_usd` as the CLI reports it.
On a subscription that is **notional** — what the work would have cost on the
API, not a charge. It is still the best available proxy for how much a card has
consumed, and `budget_remaining` and the subtree budgets use it, so the
containment rules work either way. Just do not read the total as a bill.

### Agent runs and arbiter calls are counted separately

Two things spend money: agents working cards, and the arbiter judging them.
The totals include both; the run count is agents only.

    spend     $17.10 over 15 run(s), 4 arbiter call(s) $0.36

A run means an agent worked a card, so counting a judgment as a run would make
the number mean less, not more. Arbiter spend used to be discarded entirely —
it was invisible to this figure, to subtree budgets and to `budget_remaining` —
so a board with `arbiter_judges` on a stage was spending more than it said.
`dispatch status` breaks the two out whenever any arbiter call has happened.

### Why the total lags while agents are working

A run's cost does not exist until the run ends — the agent CLI reports
`total_cost_usd` once, on its final event, and nothing before that carries a
dollar figure. So a board with agents in flight is always showing a number that
is behind.

Rather than guess, both the board and `dispatch status` say what is missing:

    spend     $861.40 over 381 run(s)  +2 still running, oldest 320s, not yet costed

Converting the streamed token counts into money would need a price table, and a
price table that drifts reports confident nonsense — which is the failure this
project has already fixed once, when arbiter spend was measured and discarded.

Rate limits are the real ceiling on a subscription. Teach
`.dispatch/gates/quota.sh` to print your remaining percentage and the
`quota_above` gate will hold cards rather than burning through it — see
`dispatch docs gates`.

Next: `dispatch docs sessions`

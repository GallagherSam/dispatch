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

Rate limits are the real ceiling on a subscription. Teach
`.dispatch/gates/quota.sh` to print your remaining percentage and the
`quota_above` gate will hold cards rather than burning through it — see
`dispatch docs gates`.

Next: `dispatch docs sessions`

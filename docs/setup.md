# Setup
> Standing up dispatch in a repo and getting the first cards moving.

Written for an agent doing the setup. Work through it in order.

## 1. Initialise

    dispatch init

Detects the test command from the project (`npm test`, `pytest -q`,
`go test ./...`, `cargo test`, `make test`) and writes `.dispatch/`. Confirm what
it detected; if it found nothing, set it:

    dispatch init --test-cmd "make check"      # or edit .dispatch/config.json

**The test command matters more than anything else here.** It is what
`tests_pass` runs, and gates are the only thing that may declare work complete.

## 2. Check the pipelines fit the project

    dispatch workflows

Four card types ship by default: `development`, `bugfix`, `chore`, `research`.
Edit them in `.dispatch/workflows.json` or in the web board's Workflows tab.
Drop stages the project has no use for — a repo with no reviewers should not
have a `review` stage that a model rubber-stamps.

## 3. Decide how much autonomy

In `.dispatch/config.json`:

- `scheduler.max_concurrent` — how many agents at once (3)
- `containment.default_budget.usd` — ceiling per card subtree (25)
- `mutation.autonomy` — `policy` (accept routine additions) | `arbiter` | `human`
- `runner.auth` — `subscription` by default, so agents use your claude.ai login
  rather than an API key that happens to be in the environment
  (`dispatch docs billing`)
- `runner.permission_mode` — `acceptEdits` with `settings.json`, or
  `bypassPermissions` to skip maintaining that allowlist
- `sandbox.enabled` — confine agents to their worktree at the OS level
  (`dispatch docs sandbox`). The default backend leaves the internet open;
  the `srt` backend also restricts egress, which blocks WebFetch

## 4. Write the cards

If you would rather describe direction than decompose it yourself, use
`dispatch intent "..."` and approve the plan an agent proposes —
`dispatch docs direction`.


One card per shippable change. See `dispatch docs cards` — a card with no
runnable acceptance check is refused before it costs anything.

    dispatch add "Add rate limiting to the public API" \
      --brief "Token bucket, 100 req/min per key, keyed on the API key header." \
      --accept "pytest tests/test_ratelimit.py passes" \
      --accept "existing endpoints keep their current latency budget" \
      --scope "src/api/**" --scope "tests/**" \
      --tag api --start

Order them with edges, don't rely on luck:

    dispatch link t_aaa t_bbb                  # bbb waits for aaa
    dispatch link t_aaa t_bbb --kind artifact  # bbb also gets aaa's output
    dispatch link t_aaa t_bbb --kind mutex     # never run these two together

## 5. Start it

    dispatch up -d          # scheduler + board on this repo's own port
    dispatch up -d --host tailscale   # ...reachable from your other devices
    dispatch status
    dispatch blocked        # if nothing is moving, this says why

## 6. Hand back to the human

Tell them: what is on the board, what will need their sign-off, the budget
ceiling, and that `dispatch needs` is where decisions queue up. Then stop —
the scheduler does not need you resident.

If you do need to wait for a card before doing dependent work, block on it
rather than polling: `dispatch wait t_abc123 --timeout 900`. See
`dispatch docs sessions`.

Next: `dispatch docs cards`

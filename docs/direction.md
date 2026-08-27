# Direction
> Describe what you want; an agent proposes the cards; you approve the plan.

Writing cards by hand means deciding the decomposition yourself, before you have
read the code as carefully as an agent will. This is the other way round: you
give direction, something reads the repo, and you approve a plan.

    dispatch intent "Rate limiting on the public API. Per API key, not per IP —
    we have customers behind shared NAT. Existing endpoints must keep their
    latency budget."

Or **Describe work** in the board's header.

## What happens

    spec ─────────────► signoff ─────────────► cards
    planner              you                   created from the plan

1. A `planner` agent reads the repository and writes a plan: a set of cards,
   each with a brief, acceptance criteria, scope and dependencies, plus the
   risks and what it deliberately left out.
2. The `has_plan` gate refuses a shapeless one before it reaches you — a card
   with no acceptance criteria, or a dependency on something not in the plan.
3. The plan arrives in **Needs You**, rendered card by card.
4. You **approve** and the cards are created, with their dependency edges, and
   the ones nothing is waiting on start. You **amend** with a note and it is
   re-planned with your objection attached. You **reject** and nothing is built.

    dispatch plan t_abc123          # read it in the terminal
    dispatch respond c_xy approve   # build it
    dispatch respond c_xy amend --note "Split the middleware card — auth and
                                        rate limiting are separate changes."

## Write direction, not tasks

The planner is better than you at decomposition because it has just read the
code. It is worse than you at knowing what you actually want. So say:

- what should change, and **why** — the reason survives contact with surprises
  the plan did not anticipate
- the constraints that matter (per key not per IP; do not change the response
  shape; this must ship before Friday)
- what you do **not** care about, so it stops deliberating

Do not say which files to touch or how many cards to make. If you know that
precisely, write the card yourself with `dispatch add`.

## The cards it makes

They are ordinary cards. They are not children of the direction card — a parent
waits for its children at its final stage, and the direction's final stage is
your approval, so parenting them would deadlock it the moment you said yes.
They carry `from:<id>` as a tag and the direction's id in their provenance, so
you can find them together:

    dispatch ls --json | python3 -c "import json,sys; [print(c['id'], c['title'])
      for c in json.load(sys.stdin) if 'from:abc123' in c['tags']]"

## When it refuses

If the request is too vague to plan, the planner returns no cards and says why,
and that escalates to you rather than inventing work. Amend with more detail.

Next: `dispatch docs memory`

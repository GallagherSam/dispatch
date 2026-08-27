# Workflows
> Card types own pipelines. Each stage names its agent and its gates.

    development:  build  ->  qa  ->  review  ->  signoff  ->  integrate
                  developer  qa    reviewer    human       integrator

A card of a given type enters at the first stage and moves right as each stage
clears its gates. The card keeps its identity the whole way — one card, many
stage runs — which is what makes the board read like a board.

## The shape

```jsonc
{"card_types": {
  "development": {
    "label": "Development", "color": "#1D6B58",
    "stages": [
      {"stage": "build",   "agent": "developer",
       "gates": ["tests_pass", "has_acceptance"]},
      {"stage": "qa",      "agent": "qa", "gates": ["tests_pass"]},
      {"stage": "review",  "agent": "reviewer", "gates": []},
      {"stage": "signoff", "agent": "human",
       "auto_pass_if": "small_and_green", "sla": "4h", "on_sla": "block"},
      {"stage": "integrate", "agent": "integrator",
       "gates": ["tests_pass"], "lock": "integration"}
    ]}}}
```

| key | meaning |
|---|---|
| `stage` | which board column, from the global list in `config.json` |
| `agent` | role from `agents.json`; `human` makes it a checkpoint |
| `gates` | extra gates for this stage, on top of the global ones |
| `lock` | a named mutex — only one card holds it at a time |
| `auto_pass_if` | human stages only: skip asking when it's trivial |
| `sla` / `on_sla` | human stages only: what happens if nobody answers |

## Columns are global, pipelines are subsets

`config.json` holds the column vocabulary; a card type walks an ordered subset of
it. That is what keeps the board coherent when types with different pipelines
share one screen. A pipeline that runs a column backwards is flagged.

## Editing

Web board → Workflows tab: reorder stages, assign agents and gates, add types.
Or edit `.dispatch/workflows.json` and `dispatch workflows import`.

    dispatch workflows                     # show every pipeline, with problems
    dispatch workflows export              # write .dispatch/workflows.json
    dispatch workflows import --file ../other-repo/.dispatch/workflows.json

Workflows round-trip as JSON on purpose: a pipeline you like moves to the next
repo intact.

## Agent roles

`.dispatch/agents.json` sets the model and tool allowlist per role; the prompt
for each lives in `.dispatch/agents/<role>.md`. Edit those prompts — they are the
cheapest lever on output quality in the whole system.

Next: `dispatch docs gates`

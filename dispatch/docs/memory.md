# Memory
> What agents have already learned about this repo, so the next one starts warm.

Every card starts a fresh agent with no history. Without this, the first minutes
of each run go on rediscovering the same things: where the tests live, which
module owns what, the gotcha that bit the last three cards.

## It is injected, not looked up

The most relevant memories are searched for using the card's title, brief, tags
and scope, and put **into the prompt** before the agent starts. An agent that
has to remember to look something up mostly does not.

Agents are also told how to search and write more:

    dispatch memory search "rate limiter"
    dispatch memory add "Where the API tests live" \
      --body "tests/api/, run with 'npm test -- api'. Fixtures in conftest.py." \
      --tags api,testing --kind pointer

## Kinds

| kind | for |
|---|---|
| `pointer` | where something lives, how to run it |
| `convention` | how this repo does a thing, and is expected to keep doing it |
| `gotcha` | the trap that has now caught more than one agent |
| `decision` | why something is the way it is, so it is not "fixed" |
| `fact` | anything else durable |

Worth writing: durable, non-obvious, verified. Not worth writing: what the code
already says plainly, anything specific to one card, or anything guessed.

## From the terminal

    dispatch memory                     # everything, newest first
    dispatch memory search "auth"       # ranked
    dispatch memory search "auth" --tags api
    dispatch memory show m_abc123
    dispatch memory rm m_abc123

## Over HTTP

The board serves it, and agents are given the URL:

    GET    /api/memory?q=rate+limiter&limit=8&tags=api
    GET    /api/memory/m_abc123
    POST   /api/memory      {"title":…, "body":…, "tags":[…], "kind":…}
    PUT    /api/memory/m_abc123
    DELETE /api/memory/m_abc123

## How search works, and its limit

SQLite FTS5 with bm25 ranking — no dependency, no model, no daemon. Repo facts
are mostly retrieved by their nouns (file names, symbols, commands), which is
what keyword search is good at.

It will not match a paraphrase that shares no words with what was written. A
vector store would; it would also add a large dependency tree to a tool that
currently has none. If recall turns out to be the limit in practice, retrieval
is one function and can be swapped without touching anything else.

Next: `dispatch docs cli`

# Sessions
> How a Claude Code session finds out a card has landed.

Two different questions hide here, and they have different answers.

**"Who decides the small things?"** The session that seeded the cards, usually —
it holds the context of the larger task, and routing those decisions through a
person makes them wait minutes for something that takes seconds. That is
`dispatch attend`, below.

**"Who decides what to build next?"** You. The board goes idle and says so; the
session checks the result against what was asked and either adds cards or
reports done.

What a session should *not* do is sit watching cards it has no decision to make
about. Blocking on `attend` is not watching — it wakes on events.

## 1. Let the session make the small decisions: `dispatch attend`

The common friction: you hand a session a large task, it seeds the cards, and
then you come back every few minutes to answer decisions the session was better
placed to make. It holds the context of what you actually asked for; you are
just relaying.

`dispatch attend` blocks until a decision is the session's, and hands over
everything needed to judge it — the card, its acceptance criteria, the agent's
account, the gate verdicts, the diff — so it never has to go digging.

The reasoning is **never** clipped: a reviewer's verdict survives a clip and the
concern buried at the end of it does not, and a decision screen that keeps the
conclusion while dropping the caveat trains you to rubber-stamp. Only the diff
is bounded, and `--full` prints all of it.

```
dispatch attend            # blocks; returns when there is something to decide
dispatch respond c_ab12 approve --as session
dispatch attend            # again
```

Exit codes drive the loop:

| code | meaning | what the session does |
|---|---|---|
| `3` | a decision that is yours | judge it, `respond`, attend again |
| `2` | still working (timed out) | attend again |
| `0` | idle and clear | check the result against the task; add cards, or report done |
| `4` | idle, but stuck on a person's decision | relay it to the operator and stop |
| `1` | cards ended badly with nothing open | report it |

**The session is not held open.** It stays alive by making tool calls, and
`attend` blocking *is* the alert.

### What a session may decide

Money, secrets and runaway detection are never a session's call; everything
else usually is. Each checkpoint records which audience may answer it, and
`respond --as session` refuses one that is not the session's rather than
letting it rubber-stamp a spend. The split is `session.may_decide` /
`session.human_only` in `config.json` — see `dispatch docs config`.

### The loop, to paste into a session

Without a channel the session drives the board by blocking:

> You are driving a dispatch board. Seed the cards for the task, then run
> `dispatch attend`. On exit 3, read the packet, judge it against what I
> actually asked for, and `dispatch respond <id> approve|amend|reject --as
> session` — amend or reject with a note saying specifically what is wrong,
> because that note becomes the next attempt's brief. Then attend again. On
> exit 2 attend again. On exit 4 tell me what needs my call and stop. On exit 0
> check the result against the task: if it is short, add cards and keep going;
> if it is done, summarise what shipped and stop.

With a channel it does the opposite — it **stops**, and the board wakes it. See
`dispatch docs channels` for that version.

## 2. Ring the doorbell: `dispatch channel`

`attend` blocks. If you would rather the session got on with something else and
be interrupted when a decision appears, a **channel** pushes into a session that
is already running — the only mechanism that can, since an MCP tool only answers
when asked.

```bash
dispatch channel --install
claude --dangerously-load-development-channels server:dispatch
```

What crosses is a pointer — "card t_abc needs a response" — never the
agent-written content behind it, which the session still fetches with `attend`.
See `dispatch docs channels`, including the research-preview caveats.

## 3. Block on one specific card: `dispatch wait`

    dispatch wait t_abc123 --timeout 900
    dispatch wait --tag api --json

Same exit codes as `attend` (`0` landed, `1` failed, `2` timed out, `3` needs a
human), and it returns on a checkpoint rather than hanging on something only a
person can move. Use it when you want *one* card before doing dependent work;
use `attend` when you want to drive the whole board.

## 4. Holding a session open: a Stop hook

`dispatch attend` covers the common case. If you want a session *held open*
between turns rather than blocking in a command, a Claude Code Stop hook is the
only thing that can push into one — MCP is request/response and cannot
interrupt.

```json
{ "hooks": { "Stop": [ { "hooks": [
  { "type": "command", "command": "dispatch hook stop" } ] } ] } }
```

Every turn end the session is handed the board's state. Add
`--block-while-busy` to keep it alive until the board settles; dispatch keeps
its own loop guard (`--max-blocks`, default 20, reset when the board goes idle)
because Claude Code passes none for Stop hooks. Use it deliberately — a session
held open is a session spending tokens.

## 5. Tell the human, not the session

`dispatch status`, `dispatch needs`, `dispatch wait --all`, and the web board.
For a card that finishes at 3am, a `post_complete` gate can notify you however
you like — see `dispatch docs gates`.


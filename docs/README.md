# dispatch — documentation

Generated from the manual that ships inside the tool. Do not edit by
hand: change `dispatch/docs/` and run `dispatch docs --export docs/`.

The same pages are available in the terminal with `dispatch docs
<topic>`, which is what agents read.

- [Overview](overview.md) — What dispatch is and the one idea it is built around.
- [Setup](setup.md) — Standing up dispatch in a repo and getting the first cards moving.
- [Cards](cards.md) — Writing a card an agent can actually finish and a gate can actually judge.
- [Workflows](workflows.md) — Card types own pipelines. Each stage names its agent and its gates.
- [Gates](gates.md) — The only thing permitted to declare work complete. Four verdicts, not a boolean.
- [Checkpoints](checkpoints.md) — Human gates. They hold their own subtree, never the whole board.
- [Proposals](proposals.md) — How a working agent tells the board about work it should not do itself.
- [Merging](merging.md) — How a finished card lands on the base branch.
- [Direction](direction.md) — Describe what you want; an agent proposes the cards; you approve the plan.
- [Memory](memory.md) — What agents have already learned about this repo, so the next one starts warm.
- [Sandbox](sandbox.md) — Confining agents to their worktree at the OS level, not by asking nicely.
- [Serving](serving.md) — Ports for several boards at once, and viewing one from another device.
- [Billing](billing.md) — Which credentials the agents use, and what the spend figures mean.
- [Sessions](sessions.md) — How a Claude Code session finds out a card has landed.
- [Channels](channels.md) — Pushing a doorbell into a session that is already running.
- [CLI](cli.md) — Every command, grouped by what you are trying to do.
- [Config](config.md) — `.dispatch/config.json`, annotated.
- [Troubleshooting](troubleshooting.md) — Nothing is running, or everything is failing.

## Notes

Point-in-time documents, not part of the manual and not kept
current.

- [handoff-2026-08-26](notes/handoff-2026-08-26.md)

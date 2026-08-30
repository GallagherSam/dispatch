# Merging
> How a finished card lands on the base branch.

Each card works on `dispatch/<id>` in its own git worktree. When its pipeline is
exhausted the card enters `merging`, and the merge worker lands it.

## The sequence

1. **serialise** — one merge at a time, ever. Not configurable; N worktrees
   converging on one branch is where these systems die.
2. **rebase** the card's branch onto the base. A conflict aborts cleanly.
3. **re-run the completion gates on the rebased tree.** This is the step that
   earns its keep: a card's tests passing in isolation says nothing about
   whether they pass on top of whatever landed while it was working.
4. **fast-forward only** into the base branch.
5. **clean up** the worktree and the branch.

Nothing is ever forced. If step 2 or 3 fails, the base branch is untouched and
the card opens a checkpoint carrying the conflict or the failing output.

## When it waits

The merge defers, without blaming the card, when:

- the repo is on a different branch than the card's base — *your* branch is
  never touched
- there are uncommitted changes to tracked files

Untracked files are irrelevant to a fast-forward and do not block anything, which
matters because `dispatch init` itself leaves `.dispatch/` untracked.

It retries every `runner.merge_retry_s` seconds (30 by default).

## Settings

```jsonc
"runner": {
  "merge_on_done": true,
  "base_branch": null,               // null: whatever the card branched from
  "delete_branch_after_merge": true,
  "merge_retry_s": 30
}
```

Set `merge_on_done: false` and cards finish on their branches for you to review
and merge by hand.

## `done` is verified, not assumed

A card that finishes its pipeline but never lands is the quietest failure this
system can produce: the board says `done`, you move on, and the work exists only
on a branch nobody looks at again. So every card at `done` is checked against
its branch, and one with commits left is put back into `merging` and logged as
`merge.unlanded`.

That covers the path that caused it: a merge conflict opens a checkpoint, you
fix the cause and approve — approval used to queue the card at stage `done`,
which nothing picks up. Approving now returns it to the merge worker whenever
there is still something to land.

## When a card will not land

It appears in `dispatch needs` with the conflict or the failing gate output. Your
options are the usual three: `amend` with instructions and let an agent redo it,
`reject` to park it, or fix the branch yourself and requeue:

    dispatch edit t_abc123 --requeue


## The integrate stage does not merge

It cannot, and it does not need to. An agent works in a git worktree whose
metadata lives in the main repository's `.git/worktrees/`, outside the writable
region of its sandbox — so `git rebase`, `git commit`, or anything that writes
an index or a ref fails with `Operation not permitted`. That is deliberate: a
worktree that could rewrite refs could rewrite your base branch.

dispatch lands the card itself, from the scheduler and outside any sandbox:
rebase onto the base, re-run the completion gates on the rebased tree,
fast-forward.

So the integrate stage answers a question instead — *will this land cleanly,
and if not, what should someone do about it?* A clean fast-forward needs
nothing from it. A conflict gets a per-file account of what clashes and which
resolution the agent would choose, which is what a human or a follow-up card
can act on. An integrator that tries to hand-materialise a merge is burning
money on something that gets thrown away; the shipped prompt says so.


Next: `dispatch docs direction`

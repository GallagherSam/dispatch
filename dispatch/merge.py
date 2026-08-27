"""Landing a finished card on the base branch.

Deliberately boring and strictly serialised: one merge at a time, rebase first,
re-run the gates *after* the rebase, then fast-forward.  N worktrees converging
on one branch is where these systems die, and the throughput gained by
parallelising it is not worth the class of bug it buys.

The re-run after rebasing is the part that earns its keep: a card's tests passed
in isolation, which says nothing about whether they pass on top of whatever
landed while it was working.
"""
from __future__ import annotations

import os
from typing import Any

from dispatch.db import DB
from dispatch.runner import _git, is_git_repo, remove_worktree

# outcomes
MERGED, CONFLICT, REJECTED, BUSY, SKIPPED = (
    "merged", "conflict", "rejected", "busy", "skipped")


def _clean(root: str) -> bool:
    """Only *tracked* changes matter.

    Untracked files cannot be clobbered by a fast-forward, and `dispatch init`
    itself leaves `.dispatch/` untracked — counting those would mean no card
    could ever land in a freshly initialised repo.
    """
    out = _git(root, "status", "--porcelain", "--untracked-files=no").stdout
    return not out.strip()


def current_branch(root: str) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def base_branch(cfg: dict[str, Any], task: dict[str, Any], root: str) -> str:
    configured = cfg.get("runner", {}).get("base_branch")
    if configured:
        return str(configured)
    return (task.get("workspace") or {}).get("base_ref") or current_branch(root)


def unlanded(root: str, cfg: dict[str, Any], task: dict[str, Any]) -> int:
    """Commits on the card's branch that are not on the base branch.

    Non-zero on a card the board calls `done` means the work exists only on a
    branch nobody will look at again — the quietest possible failure.
    """
    ws = task.get("workspace") or {}
    branch = ws.get("branch")
    if not branch or not is_git_repo(root):
        return 0
    if _git(root, "rev-parse", "--verify", branch).returncode != 0:
        return 0
    base = base_branch(cfg, task, root)
    out = _git(root, "rev-list", "--count", f"{base}..{branch}")
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return 0


def merge_card(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
               task: dict[str, Any]) -> tuple[str, str]:
    """Returns (outcome, detail).  Never raises; the caller decides what a
    failure means for the card."""
    ws = task.get("workspace") or {}
    wt, branch = ws.get("worktree"), ws.get("branch")
    base = base_branch(cfg, task, root)

    if not branch or not is_git_repo(root):
        return SKIPPED, "no branch to merge"
    # the worktree may be gone while the branch still holds the work
    if ((not wt or not os.path.isdir(wt))
            and _git(root, "rev-parse", "--verify", branch).returncode != 0):
        return SKIPPED, "branch no longer exists"

    # The base branch must be checked out and clean in the main repo. Anything
    # else is your work in progress, and dispatch does not touch it.
    if current_branch(root) != base:
        return BUSY, (f"your repo is on branch '{current_branch(root)}', not "
                      f"'{base}' — dispatch will not switch it for you")
    if not _clean(root):
        return BUSY, ("your working tree has uncommitted changes to tracked "
                      "files — commit or stash them and it will land")

    ahead = _git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
    if ahead == "0":
        return SKIPPED, "nothing to merge — the branch adds no commits"

    # 1. rebase onto the base so the merge is a fast-forward
    if wt and os.path.isdir(wt):
        r = _git(wt, "rebase", base)
        if r.returncode != 0:
            _git(wt, "rebase", "--abort")
            detail = (r.stdout + r.stderr).strip()[-3000:]
            return CONFLICT, f"rebase onto {base} conflicted\n\n{detail}"

        # 2. the gates again, on the rebased tree
        ok, why = _recheck(db, root, cfg, workflows, task, wt, base)
        if not ok:
            return REJECTED, why

    # 3. fast-forward only: if this is not a straight line, something moved
    #    under us and the next tick can try again
    r = _git(root, "merge", "--ff-only", branch)
    if r.returncode != 0:
        return BUSY, (r.stdout + r.stderr).strip()[-1500:]

    return MERGED, _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def _recheck(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
             task: dict[str, Any], wt: str, base: str) -> tuple[bool, str]:
    """Re-run the pipeline's own completion gates against the rebased tree.

    The card's stage is `done` by now, and a gate lookup against `done` finds
    nothing — so ask the last stage of the pipeline what it would have
    required. Without this the re-check is close to vacuous, which defeats
    the entire point of rebasing before landing.
    """
    from dispatch import gates as G
    from dispatch.config import paths
    from dispatch.runner import diff_against
    from dispatch.workflows import pipeline

    stages = pipeline(workflows, task["card_type"])
    judging_stage = stages[-1]["stage"] if stages else task["stage"]

    diff, files = diff_against(wt, base)
    ctx = {"db": db, "cfg": cfg, "workflows": workflows, "root": root,
           "paths": paths(root), "task": {**task, "stage": judging_stage},
           "cwd": wt, "diff": diff, "changed_files": files}
    verdict, _ = G.evaluate(ctx, "pre_complete")
    if verdict.verdict == G.PASS:
        return True, ""
    return False, (f"[{verdict.gate}] {verdict.reason}\n\n"
                   f"{verdict.evidence or ''}").strip()


def cleanup(root: str, cfg: dict[str, Any], task: dict[str, Any]) -> None:
    """Remove the worktree and, if configured, the branch it merged from."""
    remove_worktree(root, task["id"])
    if cfg.get("runner", {}).get("delete_branch_after_merge", True):
        branch = (task.get("workspace") or {}).get("branch")
        if branch:
            _git(root, "branch", "-D", branch)

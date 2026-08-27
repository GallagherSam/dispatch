"""Waiting on the board from outside it.

A session that started some cards sometimes wants to know when they land. The
answer is not for the model to poll — that spends a turn and a prompt every few
minutes to learn nothing. It is for a *command* to block, so the session makes
one tool call that happens to take a while and then returns with the answer.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from dispatch import board as B
from dispatch.db import DB

#: exit codes, so a caller can branch without parsing prose
OK, FAILED, TIMEOUT, NEEDS_HUMAN = 0, 1, 2, 3
#: `attend` reuses these: 3 means "decide this", 4 means "only a person can"
DECIDE, RELAY = 3, 4

ACTIVE_STATUSES = (B.QUEUED, B.READY, B.LEASED, B.RUNNING, B.BLOCKED, B.MERGING)


def targets(db: DB, ids: list[str] | None = None,
            tag: str | None = None,
            card_type: str | None = None) -> list[str]:
    if ids:
        return list(ids)
    out = []
    for t in B.all_tasks(db, include_terminal=False):
        if tag and tag not in (t.get("tags") or []):
            continue
        if card_type and t["card_type"] != card_type:
            continue
        out.append(t["id"])
    return out


def snapshot(db: DB, ids: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for tid in ids:
        t = B.get(db, tid)
        if t:
            out[tid] = {"stage": t["stage"], "status": t["status"],
                        "title": t["title"]}
    return out


def open_checkpoints(db: DB, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [dict(r) for r in db.q(
        f"SELECT id, task_id, question FROM checkpoints "
        f"WHERE status='open' AND task_id IN ({q})", tuple(ids))]


def settled(state: dict[str, dict[str, Any]]) -> bool:
    return all(v["status"] in B.TERMINAL for v in state.values())


def outcome(db: DB, ids: list[str],
            stop_on_checkpoint: bool = True) -> tuple[int, str]:
    state = snapshot(db, ids)
    if not state:
        return OK, "no such cards"
    if stop_on_checkpoint:
        cps = open_checkpoints(db, ids)
        if cps:
            return NEEDS_HUMAN, cps[0]["question"]
    bad = [tid for tid, v in state.items()
           if v["status"] in (B.FAILED, B.DEADLETTER, B.CANCELLED)]
    if bad:
        return FAILED, f"{len(bad)} card(s) did not land: {', '.join(bad)}"
    if settled(state):
        return OK, f"{len(state)} card(s) done"
    return -1, "still working"


def wait(db: DB, ids: list[str], *, timeout: float = 0.0,
         interval: float = 2.0, stop_on_checkpoint: bool = True,
         on_change: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None
         ) -> tuple[int, str]:
    """Block until the cards settle. Returns (exit_code, reason).

    Reports transitions as they happen so a terminal shows movement rather than
    a hang; a session watching the output learns *what* changed, not just that
    something did.
    """
    deadline = (time.time() + timeout) if timeout else None
    previous = snapshot(db, ids)
    if on_change:
        for tid, cur in previous.items():
            on_change(tid, {}, cur)

    def report(current):
        if not on_change:
            return
        for tid, cur in current.items():
            was = previous.get(tid, {})
            if (was.get("stage"), was.get("status")) != (cur["stage"], cur["status"]):
                on_change(tid, was, cur)

    while True:
        # Report before deciding to return: the transition that ends the wait
        # is the one the caller most wants to see, and checking first meant
        # never reporting it.
        current = snapshot(db, ids)
        report(current)
        previous = current

        code, reason = outcome(db, ids, stop_on_checkpoint)
        if code >= 0:
            return code, reason
        if deadline and time.time() >= deadline:
            still = [f"{t} ({v['stage']}/{v['status']})"
                     for t, v in current.items()
                     if v["status"] not in B.TERMINAL]
            return TIMEOUT, "timed out with " + ", ".join(still[:6]) + " unfinished"
        time.sleep(interval)


# ---------------------------------------------------------------------------
# board summary, for a Stop hook to hand back to a session
# ---------------------------------------------------------------------------

def board_summary(db: DB) -> dict[str, Any]:
    tasks = B.all_tasks(db)
    active = [t for t in tasks if t["status"] in ACTIVE_STATUSES]
    running = {r["task_id"] for r in db.q("SELECT task_id FROM leases")}
    cps = [dict(r) for r in db.q(
        "SELECT id, task_id, question FROM checkpoints WHERE status='open'")]
    recent = [dict(r) for r in db.q(
        "SELECT task_id, ts FROM events WHERE kind='task.done' "
        "ORDER BY id DESC LIMIT 10")]
    by_id = {t["id"]: t for t in tasks}
    return {
        "active": [{"id": t["id"], "title": t["title"], "stage": t["stage"],
                    "status": t["status"], "running": t["id"] in running}
                   for t in active],
        "checkpoints": [{**c, "title": (by_id.get(c["task_id"]) or {}).get("title")}
                        for c in cps],
        "recently_done": [{"id": r["task_id"],
                           "title": (by_id.get(r["task_id"]) or {}).get("title")}
                          for r in recent],
        "idle": not active,
    }


def summary_text(s: dict[str, Any]) -> str:
    if s["idle"] and not s["checkpoints"]:
        done = ", ".join(d["id"] for d in s["recently_done"][:5])
        return ("The dispatch board is idle — nothing is queued or running."
                + (f" Recently completed: {done}." if done else ""))
    lines = []
    if s["active"]:
        lines.append(f"{len(s['active'])} card(s) still on the dispatch board:")
        for t in s["active"][:10]:
            mark = "running" if t["running"] else t["status"]
            lines.append(f"  {t['id']} [{t['stage']}/{mark}] {t['title'][:60]}")
    if s["checkpoints"]:
        lines.append(f"{len(s['checkpoints'])} checkpoint(s) waiting on a human:")
        for c in s["checkpoints"][:10]:
            lines.append(f"  {c['id']} — {c['question'][:80]}")
        lines.append("Answer with: dispatch respond <id> approve|amend|reject "
                     "--note \"...\"")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# attending: a session sitting in the loop instead of a person
# ---------------------------------------------------------------------------

def open_for(db: DB, audience: str) -> list[dict[str, Any]]:
    """Open checkpoints this audience may answer, oldest first."""
    allowed = ("session", "any") if audience == "session" else ("human", "any")
    q = ",".join("?" * len(allowed))
    rows = db.q(f"SELECT * FROM checkpoints WHERE status='open' "
                f"AND COALESCE(audience,'any') IN ({q}) ORDER BY created_at",
                allowed)
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["bundle"] = json.loads(d.get("bundle") or "{}")
        except ValueError:
            d["bundle"] = {}
        out.append(d)
    return out


def decision_packet(db: DB, cp: dict[str, Any]) -> dict[str, Any]:
    """Everything needed to decide, so the session never has to go digging.

    A decision made from a card id alone is a coin flip; the point of routing
    this to the session that holds the larger task is that it can judge the
    work against it.
    """
    from dispatch import board as B
    task = B.get(db, cp["task_id"]) or {}
    b = cp.get("bundle") or {}
    runs = [dict(r) for r in db.q(
        "SELECT id, stage, agent_type, attempt, exit_code, usd, summary, log_dir "
        "FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 3",
        (cp["task_id"],))]
    diff_file = None
    if runs and runs[0].get("log_dir"):
        import os as _os
        candidate = _os.path.join(runs[0]["log_dir"], "diff.patch")
        diff_file = candidate if _os.path.exists(candidate) else None
    gates = [dict(r) for r in db.q(
        "SELECT gate, verdict, reason FROM gate_runs WHERE task_id=? "
        "ORDER BY id DESC LIMIT 10", (cp["task_id"],))]
    return {
        "checkpoint": {"id": cp["id"], "question": cp["question"],
                       "kind": cp.get("kind"), "topic": cp.get("topic"),
                       "audience": cp.get("audience", "any"),
                       "waiting_since": cp["created_at"]},
        "card": {"id": task.get("id"), "title": task.get("title"),
                 "card_type": task.get("card_type"), "stage": task.get("stage"),
                 "brief": task.get("brief"),
                 "acceptance": task.get("acceptance") or [],
                 "attempts": task.get("attempts"),
                 "branch": (task.get("workspace") or {}).get("branch")},
        "what_happened": {
            "summary": b.get("summary") or (runs[0]["summary"] if runs else None),
            "evidence": b.get("evidence") or task.get("last_evidence"),
            "changed_files": b.get("changed_files") or [],
            "diff": b.get("diff") or "", "diff_file": diff_file,
            "gates": gates, "runs": runs,
            "plan": b.get("plan"),
            "reason": b.get("reason"), "note": b.get("note"),
        },
        "options": b.get("options") or ["approve", "amend", "reject"],
    }


def attend(db: DB, *, timeout: float = 480.0, interval: float = 2.0,
           audience: str = "session") -> tuple[int, dict[str, Any] | None]:
    """Block until there is a decision for this audience, or the board settles.

    Returns (code, packet):
      DECIDE   a decision this audience may make — packet has everything
      RELAY    the board is idle but stuck on someone else's decision
      OK       idle and clear
      FAILED   cards ended badly and nothing is open about it
      TIMEOUT  still working; call again
    """
    from dispatch import board as B
    deadline = time.time() + timeout if timeout else None
    other = "human" if audience == "session" else "session"

    while True:
        mine = open_for(db, audience)
        if mine:
            return DECIDE, decision_packet(db, mine[0])

        active = [t for t in B.all_tasks(db, include_terminal=False)
                  if t["status"] in ACTIVE_STATUSES]
        if not active:
            theirs = open_for(db, other)
            if theirs:
                return RELAY, decision_packet(db, theirs[0])
            bad = [t for t in B.all_tasks(db)
                   if t["status"] in (B.DEADLETTER, B.FAILED)]
            if bad:
                return FAILED, {"failed": [{"id": t["id"], "title": t["title"],
                                            "reason": t.get("block_reason")}
                                           for t in bad]}
            return OK, None

        if deadline and time.time() >= deadline:
            return TIMEOUT, {"working": [
                {"id": t["id"], "title": t["title"], "stage": t["stage"],
                 "status": t["status"]} for t in active[:8]]}
        time.sleep(interval)

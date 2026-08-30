"""Board operations: cards, edges, the ready set, and stage advancement.

Nothing in here decides *when* to act — that is the scheduler's job.  This
module only answers questions and applies transitions.
"""
from __future__ import annotations

import json
import os
from typing import Any

from dispatch.db import DB, new_id, now, row_to_task

# status vocabulary
QUEUED, READY, LEASED, RUNNING = "queued", "ready", "leased", "running"
BLOCKED, CHECKPOINT, DONE, FAILED = "blocked", "checkpoint", "done", "failed"
CANCELLED, DEADLETTER, MERGING = "cancelled", "deadletter", "merging"

ACTIVE = (QUEUED, READY, LEASED, RUNNING, BLOCKED, CHECKPOINT, MERGING)
TERMINAL = (DONE, FAILED, CANCELLED, DEADLETTER)

_JSON_COLS = {"acceptance", "tags", "artifacts", "gates", "budget", "spent",
              "workspace"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create(db: DB, cfg: dict[str, Any], workflows: dict[str, Any], *,
           title: str, brief: str = "", card_type: str = "development",
           acceptance: list[str] | None = None, parent_id: str | None = None,
           tags: list[str] | None = None, priority: int = 50,
           stage: str | None = None, agent_type: str | None = None,
           model: str | None = None,
           scope: list[str] | None = None, budget: dict[str, Any] | None = None,
           depends_on: list[str] | None = None,
           provenance: str = "human", proposal_id: str | None = None,
           max_attempts: int = 3) -> str:
    from dispatch.workflows import first_stage

    tid = new_id("t")
    ts = now()
    if stage is None:
        stage = "backlog"
    entry = first_stage(workflows, card_type) or {}
    ws = {"scope": scope or []}
    db.x(
        "INSERT INTO tasks (id,title,brief,acceptance,card_type,stage,agent_type,"
        "model,status,parent_id,priority,tags,max_attempts,budget,workspace,"
        "provenance,proposal_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, title, brief, json.dumps(acceptance or []), card_type, stage,
         agent_type or entry.get("agent"), model or None, QUEUED, parent_id, priority,
         json.dumps(tags or []), max_attempts, json.dumps(budget or {}),
         json.dumps(ws), provenance, proposal_id, ts, ts),
    )
    for dep in (depends_on or []):
        link(db, dep, tid, "finish_to_start")
    db.emit("task.created", tid, actor=provenance, title=title, card_type=card_type)
    return tid


def get(db: DB, task_id: str) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
    return row_to_task(r) if r else None


def all_tasks(db: DB, include_terminal: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tasks"
    if not include_terminal:
        sql += " WHERE status NOT IN ('done','cancelled')"
    return [row_to_task(r) for r in db.q(sql + " ORDER BY priority DESC, created_at")]


def update(db: DB, task_id: str, actor: str = "human", **fields: Any) -> None:
    if not fields:
        return
    if "model" in fields and not fields["model"]:
        # the web form posts "" for a cleared field and the CLI passes None;
        # both mean "fall back to the stage", and only NULL makes that true
        # everywhere that reads the column.
        fields["model"] = None
    sets, args = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        args.append(json.dumps(v) if k in _JSON_COLS else v)
    sets.append("updated_at=?")
    args.extend([now(), task_id])
    db.x(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
    db.emit("task.updated", task_id, actor=actor,
            fields={k: v for k, v in fields.items() if k != "brief"})


def link(db: DB, src: str, dst: str, kind: str = "finish_to_start",
         note: str | None = None) -> str | None:
    """Add an edge.  Refuses to create a cycle — reject the edge, don't be clever."""
    if src == dst:
        return None
    if kind == "finish_to_start":
        if _reaches(db, dst, src):
            raise ValueError(f"{src} -> {dst} would create a cycle")
        if _is_ancestor(db, src, dst):
            raise ValueError(
                f"{dst} is inside {src}, so {src} cannot finish until {dst} "
                f"does — this edge would deadlock both. Depend on a sibling, "
                f"or move {dst} out from under {src}.")
    eid = new_id("e")
    try:
        db.x("INSERT INTO edges (id,src,dst,kind,note) VALUES (?,?,?,?,?)",
             (eid, src, dst, kind, note))
    except Exception:
        return None
    db.emit("edge.added", dst, src=src, kind=kind)
    return eid


def unlink(db: DB, src: str, dst: str, kind: str = "finish_to_start") -> None:
    db.x("DELETE FROM edges WHERE src=? AND dst=? AND kind=?", (src, dst, kind))
    db.emit("edge.removed", dst, src=src, kind=kind)


def _is_ancestor(db: DB, maybe_parent: str, task_id: str) -> bool:
    cur, guard = task_id, 0
    while cur and guard < 50:
        row = db.q1("SELECT parent_id FROM tasks WHERE id=?", (cur,))
        if not row or not row["parent_id"]:
            return False
        if row["parent_id"] == maybe_parent:
            return True
        cur = row["parent_id"]
        guard += 1
    return False


def _reaches(db: DB, start: str, target: str) -> bool:
    """Is `target` reachable from `start` along finish_to_start edges?"""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        for r in db.q("SELECT dst FROM edges WHERE src=? AND kind='finish_to_start'", (cur,)):
            stack.append(r["dst"])
    return False


def deps_of(db: DB, task_id: str, kind: str = "finish_to_start") -> list[str]:
    return [r["src"] for r in
            db.q("SELECT src FROM edges WHERE dst=? AND kind=?", (task_id, kind))]


def dependents_of(db: DB, task_id: str, kind: str = "finish_to_start") -> list[str]:
    return [r["dst"] for r in
            db.q("SELECT dst FROM edges WHERE src=? AND kind=?", (task_id, kind))]


def children_of(db: DB, task_id: str) -> list[dict[str, Any]]:
    return [row_to_task(r) for r in
            db.q("SELECT * FROM tasks WHERE parent_id=?", (task_id,))]


def depth_of(db: DB, task_id: str | None) -> int:
    d, cur, guard = 0, task_id, 0
    while cur and guard < 50:
        row = db.q1("SELECT parent_id FROM tasks WHERE id=?", (cur,))
        if not row or not row["parent_id"]:
            break
        d += 1
        cur = row["parent_id"]
        guard += 1
    return d


def subtree_ids(db: DB, root_id: str) -> list[str]:
    out, stack = [], [root_id]
    while stack:
        cur = stack.pop()
        out.append(cur)
        for c in db.q("SELECT id FROM tasks WHERE parent_id=?", (cur,)):
            stack.append(c["id"])
    return out


def root_of(db: DB, task_id: str) -> str:
    cur, guard = task_id, 0
    while guard < 50:
        row = db.q1("SELECT parent_id FROM tasks WHERE id=?", (cur,))
        if not row or not row["parent_id"]:
            return cur
        cur = row["parent_id"]
        guard += 1
    return cur


def subtree_budget(db: DB, cfg: dict[str, Any], task_id: str
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """A parent's budget is the ceiling for everything beneath it — the primary
    defence against runaway expansion."""
    root = root_of(db, task_id)
    row = db.q1("SELECT budget FROM tasks WHERE id=?", (root,))
    cap = json.loads(row["budget"]) if row else {}
    if not cap:
        cap = dict(cfg.get("containment", {}).get("default_budget", {}))
    ids = subtree_ids(db, root)
    return cap, {"usd": spend(db, ids)["usd"]}


def spend(db: DB, task_ids: list[str] | None = None) -> dict[str, Any]:
    """What has actually been spent, agents and arbiter both.

    Arbiter cost used to be discarded at the point of the call, so every
    judgment, adjudication and triage was free as far as the board, the
    subtree budgets and the `budget_remaining` gate were concerned. They were
    not free. `runs` still counts only agent runs — a run means an agent worked
    a card, and inflating that number to make the money add up would just move
    the lie somewhere else.
    """
    where, args = "", ()
    if task_ids is not None:
        if not task_ids:
            return {"usd": 0.0, "agent_usd": 0.0, "arbiter_usd": 0.0,
                    "runs": 0, "arbiter_calls": 0}
        where = f" WHERE task_id IN ({','.join('?' * len(task_ids))})"
        args = tuple(task_ids)
    r = db.q1(f"SELECT COALESCE(SUM(usd),0) usd, COUNT(*) n FROM runs{where}", args)
    a = db.q1("SELECT COALESCE(SUM(usd),0) usd, COUNT(*) n FROM arbiter_calls"
              + where, args)
    agent, arb = float(r["usd"] or 0.0), float(a["usd"] or 0.0)
    return {"usd": round(agent + arb, 4), "agent_usd": round(agent, 4),
            "arbiter_usd": round(arb, 4), "runs": r["n"],
            "arbiter_calls": a["n"]}


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

def blockers(db: DB, cfg: dict[str, Any], workflows: dict[str, Any],
             task: dict[str, Any]) -> list[str]:
    """Everything structurally holding a card, before gates are consulted.

    This is what powers the 'why is nothing running?' panel, which is nearly
    free to build because the scheduler computes it every tick anyway.
    """
    from dispatch.workflows import stage_entry
    out: list[str] = []
    if task["status"] in TERMINAL:
        return ["terminal"]
    if task["status"] == CHECKPOINT:
        return ["awaiting your sign-off"]
    if task["status"] == MERGING:
        why = task.get("defer_reason") or ""
        if why.startswith("merge: "):
            return [f"cannot land yet — {why[len('merge: '):]}"]
        return ["waiting to land on the base branch"]
    if task["status"] == RUNNING or task["status"] == LEASED:
        return ["already running"]
    if task["defer_until"] and task["defer_until"] > now():
        wait = int(task["defer_until"] - now())
        out.append(f"deferred {wait}s: {task['defer_reason'] or 'gate said not yet'}")
    if task["stage"] == "backlog":
        out.append("in backlog — not yet started")
    for dep in deps_of(db, task["id"]):
        d = db.q1("SELECT id,title,status FROM tasks WHERE id=?", (dep,))
        if d and d["status"] not in (DONE,):
            out.append(f"waits on {d['id']} ({d['title'][:40]}) — {d['status']}")
    entry = stage_entry(workflows, task["card_type"], task["stage"])
    # Children hold a parent only where it would otherwise complete — a card is
    # free to move through its own pipeline while work it spawned runs beside it.
    pipe = [e["stage"] for e in workflows.get(task["card_type"], {}).get("stages", [])]
    at_last_stage = bool(pipe) and task["stage"] == pipe[-1]
    if at_last_stage:
        kids = children_of(db, task["id"])
        pending = [k["id"] for k in kids if k["status"] not in TERMINAL]
        if pending:
            out.append(f"would complete, but {len(pending)} child card(s) are "
                       f"unfinished: {', '.join(pending[:4])}")
    if task["stage"] != "backlog" and entry is None:
        out.append(f"stage '{task['stage']}' is not in the {task['card_type']} pipeline")
    lock = (entry or {}).get("lock")
    if lock:
        held = db.q1("SELECT task_id FROM locks WHERE name=? AND task_id IS NOT NULL", (lock,))
        if held and held["task_id"] != task["id"]:
            out.append(f"lock '{lock}' held by {held['task_id']}")
    return out


def ready_set(db: DB, cfg: dict[str, Any], workflows: dict[str, Any]
              ) -> list[dict[str, Any]]:
    """Cards with no structural blockers, highest priority first."""
    cands = [row_to_task(r) for r in db.q(
        "SELECT * FROM tasks WHERE status IN (?,?,?) ORDER BY priority DESC, created_at",
        (QUEUED, READY, BLOCKED))]
    out = []
    for t in cands:
        if not blockers(db, cfg, workflows, t):
            out.append(t)
    return out


def start_card(db: DB, workflows: dict[str, Any], task_id: str,
               actor: str = "human") -> None:
    """Move a backlog card onto the first stage of its pipeline."""
    from dispatch.workflows import first_stage
    t = get(db, task_id)
    if not t:
        return
    entry = first_stage(workflows, t["card_type"])
    if not entry:
        update(db, task_id, actor=actor, status=BLOCKED,
               block_reason=f"card type '{t['card_type']}' has an empty pipeline")
        return
    update(db, task_id, actor=actor, stage=entry["stage"],
           agent_type=t.get("agent_type") or entry.get("agent"),
           status=QUEUED, attempts=0, started_at=t.get("started_at") or now())
    db.emit("task.started", task_id, actor=actor, stage=entry["stage"])


def advance(db: DB, cfg: dict[str, Any], workflows: dict[str, Any],
            task_id: str, actor: str = "scheduler") -> str:
    """Stage cleared its gates.  Move right, or finish.

    A card keeps its identity as it moves — one card, many stage runs — which is
    what makes the board read the way a kanban board should.
    """
    from dispatch.workflows import next_stage
    t = get(db, task_id)
    if not t:
        return DONE
    nxt = next_stage(workflows, t["card_type"], t["stage"])
    if nxt is None:
        # The pipeline is exhausted, but the work is not finished until it has
        # landed on the base branch. The merge worker takes it from here.
        if _wants_merge(cfg, t, workflows):
            update(db, task_id, actor=actor, stage="done", status=MERGING,
                   attempts=0, block_reason=None, defer_until=0,
                   defer_reason=None)
            db.emit("task.ready_to_merge", task_id, actor=actor,
                    branch=(t.get("workspace") or {}).get("branch"))
            return MERGING
        update(db, task_id, actor=actor, stage="done", status=DONE,
               completed_at=now(), attempts=0, block_reason=None)
        db.emit("task.done", task_id, actor=actor)
        _maybe_complete_parent(db, cfg, workflows, t.get("parent_id"), actor)
        return DONE
    update(db, task_id, actor=actor, stage=nxt["stage"],
           agent_type=nxt.get("agent"), status=QUEUED, attempts=0,
           defer_until=0, defer_reason=None, block_reason=None, last_evidence=None)
    db.emit("task.advanced", task_id, actor=actor,
            to=nxt["stage"], agent=nxt.get("agent"))
    return nxt["stage"]


def _has_unlanded_work(db: DB, cfg: dict[str, Any],
                       task: dict[str, Any]) -> bool:
    root = os.path.dirname(os.path.dirname(db.path))
    try:
        from dispatch import merge as M
        return M.unlanded(root, cfg, task) > 0
    except Exception:
        return False


def _wants_merge(cfg: dict[str, Any], task: dict[str, Any],
                 workflows: dict[str, Any] | None = None) -> bool:
    if not cfg.get("runner", {}).get("merge_on_done", True):
        return False
    # Some card types produce no code — planning, research. Sending them
    # through a merge leaves a stray branch and a pointless round trip.
    if workflows is not None:
        wf = workflows.get(task.get("card_type")) or {}
        if wf.get("merge") is False:
            return False
    return bool((task.get("workspace") or {}).get("branch"))


def mark_merged(db: DB, cfg: dict[str, Any], workflows: dict[str, Any],
                task_id: str, detail: str = "", actor: str = "scheduler") -> None:
    t = get(db, task_id)
    update(db, task_id, actor=actor, status=DONE, stage="done",
           completed_at=now(), block_reason=None)
    db.emit("task.merged", task_id, actor=actor, commit=detail)
    db.emit("task.done", task_id, actor=actor)
    if t:
        _maybe_complete_parent(db, cfg, workflows, t.get("parent_id"), actor)


def _maybe_complete_parent(db: DB, cfg, workflows, parent_id: str | None,
                           actor: str) -> None:
    if not parent_id:
        return
    kids = children_of(db, parent_id)
    if kids and all(k["status"] in TERMINAL for k in kids):
        p = get(db, parent_id)
        if p and p["status"] not in TERMINAL and p["stage"] != "backlog":
            db.emit("parent.children_complete", parent_id, actor=actor)


def materialise_plan(db: DB, cfg: dict[str, Any], workflows: dict[str, Any],
                     task: dict[str, Any], actor: str = "human") -> list[str]:
    """Turn an approved plan into cards.

    The cards are not children of the direction card: a parent waits for its
    children at its final stage, and the direction's final stage is the
    approval itself — parenting them would deadlock it the moment you said yes.
    They carry its id in their provenance and a shared tag instead.
    """
    raw = task.get("plan")
    if not raw:
        return []
    plan = json.loads(raw) if isinstance(raw, str) else raw
    specs = plan.get("cards") or []
    short = task["id"].split("_")[-1]
    by_ref: dict[str, str] = {}
    made: list[str] = []

    for spec in specs:
        tags = list(spec.get("tags") or [])
        if f"from:{short}" not in tags:
            tags.append(f"from:{short}")
        tid = create(
            db, cfg, workflows,
            title=spec.get("title") or "(untitled)",
            brief=spec.get("brief", ""),
            card_type=spec.get("card_type") or "development",
            acceptance=spec.get("acceptance") or [],
            tags=tags, priority=int(spec.get("priority", 50)),
            scope=spec.get("scope") or [],
            provenance=f"intent:{task['id']}")
        if spec.get("ref"):
            by_ref[spec["ref"]] = tid
        made.append(tid)

    for spec, tid in zip(specs, made):
        for dep in spec.get("depends_on") or []:
            src = by_ref.get(dep)
            if not src or src == tid:
                continue
            try:
                link(db, src, tid)
            except ValueError:
                pass

    # start only what nothing else is waiting on; the rest follow their edges
    for tid in made:
        if not deps_of(db, tid):
            start_card(db, workflows, tid, actor=actor)

    db.emit("plan.materialised", task["id"], actor=actor, cards=made,
            count=len(made))
    return made


def audience_for(cfg: dict[str, Any], kind: str,
                 topic: str | None) -> str:
    """Who is competent to answer this. `session` and `human` are exclusive;
    `any` means whoever gets there first."""
    sess = cfg.get("session", {})
    topic = (topic or kind or "").strip()
    if topic in (sess.get("human_only") or []):
        return "human"
    if topic in (sess.get("may_decide") or []):
        return "session"
    return "any"


def open_checkpoint(db: DB, task_id: str, question: str,
                    bundle: dict[str, Any] | None = None,
                    sla_s: float | None = None, actor: str = "scheduler",
                    kind: str = "signoff", topic: str | None = None,
                    cfg: dict[str, Any] | None = None) -> str:
    """A human gate.  It blocks its own subtree via ordinary dependency edges —
    the rest of the board keeps running, which is the whole point.

    `kind` records why it opened, which decides what approval means:
      signoff     the stage's work is finished and you are accepting it
      escalation  a gate stopped the card before it ran; approving lets it run
    """
    cid = new_id("c")
    who = audience_for(cfg or {}, kind, topic or kind)
    db.x("INSERT INTO checkpoints (id,task_id,kind,audience,topic,question,"
         "bundle,sla_s,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
         (cid, task_id, kind, who, topic, question,
          json.dumps(bundle or {}, default=str), sla_s, now()))
    # A dead-lettered card keeps that status: it needs you *and* it is
    # quarantined, and the quarantine is the more useful thing to see on the
    # board. It still appears in Needs You, which reads the checkpoint table.
    current = db.q1("SELECT status FROM tasks WHERE id=?", (task_id,))
    if not current or current["status"] not in (DEADLETTER, CANCELLED):
        update(db, task_id, actor=actor, status=CHECKPOINT)
    db.emit("checkpoint.opened", task_id, actor=actor, checkpoint_id=cid,
            checkpoint_kind=kind, audience=who, topic=topic, question=question)
    return cid


def resolve_checkpoint(db: DB, cfg: dict[str, Any], workflows: dict[str, Any],
                       checkpoint_id: str, response: str, note: str = "",
                       actor: str = "human") -> None:
    """approve | reject | amend.  A rejection reason becomes the retry's brief,
    so your objection is literally the next agent's instruction."""
    row = db.q1("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,))
    if not row or row["status"] != "open":
        return
    tid = row["task_id"]
    db.x("UPDATE checkpoints SET status=?, response=?, response_note=?, resolved_at=? "
         "WHERE id=?",
         ({"approve": "approved", "reject": "rejected", "amend": "amended"}
          .get(response, response), response, note, now(), checkpoint_id))
    db.emit("checkpoint.resolved", tid, actor=actor, response=response, note=note)

    if str(row["question"] or "").startswith("Expansion alarm"):
        # Acknowledging it has to actually clear it: the ratio was historical,
        # so an answered alarm used to re-fire on every restart and the only
        # way through was to disable the guard.
        from dispatch import proposals as P
        from dispatch.config import load_config, save_config
        P.acknowledge_expansion(db, actor=actor)
        live = load_config(os.path.dirname(os.path.dirname(db.path)))
        live["scheduler"]["paused"] = False
        live["scheduler"]["paused_reason"] = None
        save_config(os.path.dirname(os.path.dirname(db.path)), live)
        db.emit("scheduler.resumed", actor=actor, why="expansion alarm answered")

    t = get(db, tid)
    if not t:
        return
    # sqlite3.Row has no .get — a lint autofix rewrote this once and every
    # checkpoint resolution broke. Boards created before `kind` existed still
    # come back without the column.
    ck_kind = (row["kind"] if "kind" in row.keys() else "signoff") or "signoff"

    # A direction card is approved into existence: the plan becomes the cards.
    if response == "approve" and t.get("plan"):
        made = materialise_plan(db, cfg, workflows, t, actor=actor)
        update(db, tid, actor=actor, status=QUEUED)
        advance(db, cfg, workflows, tid, actor=actor)
        db.emit("intent.accepted", tid, actor=actor, cards=made)
        return
    if response == "amend" and t.get("plan"):
        # send it back to be re-planned, with the objection attached
        stages = [e["stage"] for e in
                  workflows.get(t["card_type"], {}).get("stages", [])]
        update(db, tid, actor=actor,
               stage=stages[0] if stages else t["stage"],
               status=QUEUED, attempts=0, plan=None,
               last_evidence="The plan was not accepted as written:\n" +
                             (note or "(no reason given)"))
        db.emit("intent.replan", tid, actor=actor, note=note)
        return

    if response == "approve":
        if ck_kind == "escalation":
            # The card never ran this stage. Approving clears the block and lets
            # it run — advancing here would silently skip the work.
            #
            # Unless it had already finished and failed to *land*: then queuing
            # it at stage `done` is a state nothing ever picks up again, and the
            # card sits there looking successful with its work stranded on a
            # branch. Send it back to the merge worker instead.
            if _has_unlanded_work(db, cfg, t):
                update(db, tid, actor=actor, status=MERGING, attempts=0,
                       defer_until=0, defer_reason=None, block_reason=None)
                db.emit("merge.retry", tid, actor=actor)
            else:
                update(db, tid, actor=actor, status=QUEUED, attempts=0,
                       defer_until=0, defer_reason=None, block_reason=None)
        else:
            update(db, tid, actor=actor, status=QUEUED)
            advance(db, cfg, workflows, tid, actor=actor)
    elif response == "amend":
        brief = t["brief"]
        if note:
            brief = brief + "\n\n## Amended by you\n" + note
        update(db, tid, actor=actor, brief=brief, status=QUEUED, attempts=0,
               last_evidence=None)
    elif ck_kind == "escalation":
        # Rejecting an escalation means "do not do this as it stands" — park the
        # card with your reason rather than sending it back to an earlier stage
        # it never reached.
        update(db, tid, actor=actor, status=BLOCKED,
               block_reason=note or "rejected at escalation",
               last_evidence=note or None)
        db.emit("task.blocked", tid, actor=actor, reason=note)
    else:  # reject
        from dispatch.workflows import pipeline
        stages = [e["stage"] for e in pipeline(workflows, t["card_type"])]
        back = stages[0] if stages else t["stage"]
        cur = t["stage"]
        if cur in stages:
            i = stages.index(cur)
            back = stages[max(0, i - 1)]
        update(db, tid, actor=actor, stage=back, status=QUEUED, attempts=0,
               last_evidence="Rejected at sign-off:\n" + (note or "(no reason given)"))
        db.emit("task.rejected", tid, actor=actor, back_to=back)


def cancel(db: DB, task_id: str, actor: str = "human", cascade: bool = True,
           reason: str | None = None) -> None:
    """Cancelling three superseded cards and leaving no trace of why is how a
    board becomes unreadable a week later. The reason goes on the card and in
    the log."""
    ids = subtree_ids(db, task_id) if cascade else [task_id]
    for i in ids:
        db.x("UPDATE tasks SET status=?, block_reason=?, updated_at=? WHERE id=? "
             "AND status NOT IN ('done','cancelled')",
             (CANCELLED, reason, now(), i))
        db.x("DELETE FROM leases WHERE task_id=?", (i,))
    db.emit("task.cancelled", task_id, actor=actor, cascade=cascade,
            count=len(ids), reason=reason)


def acquire_lock(db: DB, name: str, task_id: str) -> bool:
    row = db.q1("SELECT task_id FROM locks WHERE name=?", (name,))
    if row and row["task_id"] and row["task_id"] != task_id:
        return False
    db.x("INSERT INTO locks (name,task_id,acquired_at) VALUES (?,?,?) "
         "ON CONFLICT(name) DO UPDATE SET task_id=excluded.task_id, "
         "acquired_at=excluded.acquired_at", (name, task_id, now()))
    return True


def release_lock(db: DB, name: str, task_id: str | None = None) -> None:
    if task_id:
        db.x("UPDATE locks SET task_id=NULL WHERE name=? AND task_id=?", (name, task_id))
    else:
        db.x("UPDATE locks SET task_id=NULL WHERE name=?", (name,))

"""Workers propose, the board disposes.

Agents never write to the board.  Direct writes would let an agent unblock
itself, redefine its own acceptance criteria, or fan out without limit.  Every
change from a worker arrives as a proposal and climbs a ladder:

    1. policy   deterministic rules — most proposals, zero model cost
    2. arbiter  one LLM call with the relevant board slice
    3. human    a checkpoint card in Needs You
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any

from dispatch.db import DB, new_id, now

KINDS = ("add_task", "split", "add_dep", "amend_brief", "raise_blocker",
         "request_gate", "cancel", "escalate")


def submit(db: DB, *, from_task: str | None, kind: str, payload: dict[str, Any],
           rationale: str = "", confidence: float | None = None,
           urgency: str = "normal") -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown proposal kind '{kind}' (expected one of {', '.join(KINDS)})")
    pid = new_id("p")
    db.x("INSERT INTO proposals (id,from_task,kind,payload,rationale,confidence,"
         "urgency,created_at) VALUES (?,?,?,?,?,?,?,?)",
         (pid, from_task, kind, json.dumps(payload, default=str), rationale,
          confidence, urgency, now()))
    db.emit("proposal.submitted", from_task, actor=f"agent:{from_task}",
            proposal_id=pid, kind=kind, rationale=rationale[:400])
    return pid


def pending(db: DB) -> list[dict[str, Any]]:
    return [dict(r) for r in
            db.q("SELECT * FROM proposals WHERE status='pending' ORDER BY created_at")]


# ---------------------------------------------------------------------------
# invariants — enforced regardless of tier
# ---------------------------------------------------------------------------

def check_invariants(db: DB, cfg: dict[str, Any], prop: dict[str, Any]
                     ) -> tuple[bool, str]:
    """The integrity core.  These five rules are what stop a fleet of agents
    from talking itself into a four-hundred-card backlog."""
    from dispatch import board as B
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) else prop["payload"]
    src = prop.get("from_task")
    cont = cfg.get("containment", {})

    # 1. an agent may not touch its own gates or acceptance criteria
    if prop["kind"] == "amend_brief":
        target = payload.get("task_id") or src
        if target == src and ({"gates", "acceptance"} & set(payload)):
            return False, "an agent may not modify its own gates or acceptance criteria"

    # 2. an agent may not mark its own card done
    if payload.get("status") in ("done", "cancelled") and payload.get("task_id") == src:
        return False, "an agent may not complete its own card — only a gate may"

    # 3. no cycles
    if prop["kind"] == "add_dep":
        a, b = payload.get("src"), payload.get("dst")
        if a and b and B._reaches(db, b, a):
            return False, f"{a} -> {b} would create a cycle"

    # 4. containment: depth and fan-out
    if prop["kind"] in ("add_task", "split"):
        parent = payload.get("parent_id") or src
        if parent:
            if B.depth_of(db, parent) + 1 > int(cont.get("max_depth", 3)):
                return False, (f"decomposition depth would exceed "
                               f"{cont.get('max_depth')}")
            n = len(B.children_of(db, parent))
            adding = len(payload.get("tasks", [])) or 1
            if n + adding > int(cont.get("max_children_per_parent", 12)):
                return False, (f"{parent} would exceed "
                               f"{cont.get('max_children_per_parent')} children")

        # 5. budget: a proposal cannot exceed the parent subtree's remaining pool
        if src:
            cap, spent = B.subtree_budget(db, cfg, src)
            if cap.get("usd") is not None and spent.get("usd", 0) >= cap["usd"]:
                return False, "subtree budget already exhausted"
    return True, ""


_STOPWORDS = {"a", "an", "the", "to", "for", "of", "in", "on", "and", "or",
              "add", "fix", "update", "make", "set", "use", "with", "from",
              "into", "that", "this", "it", "is", "be", "should", "when"}


def _tokens(text: str) -> set:
    out = set()
    for word in re.split(r"[^a-z0-9_.\-]+", (text or "").lower()):
        if word and word not in _STOPWORDS and len(word) > 1:
            out.add(word)
    return out


def similarity(a: str, b: str) -> float:
    """Character similarity misses a reworded title; token overlap misses a
    reordered one. Take whichever is more suspicious."""
    seq = difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    ta, tb = _tokens(a), _tokens(b)
    jac = (len(ta & tb) / len(ta | tb)) if (ta or tb) else 0.0
    return max(seq, jac)


def find_similar(db: DB, title: str, brief: str = "") -> tuple[str | None, float]:
    """The most suspicious existing card, and how suspicious it is."""
    if not title:
        return None, 0.0
    rows = db.q("SELECT id,title,brief FROM tasks WHERE status NOT IN ('cancelled')")
    best, best_id = 0.0, None
    for r in rows:
        score = similarity(title, r["title"] or "")
        if brief and r["brief"]:
            score = max(score, similarity(brief[:400], (r["brief"] or "")[:400]) * 0.9)
        if score > best:
            best, best_id = score, r["id"]
    return best_id, best


def find_duplicate(db: DB, title: str, threshold: float) -> str | None:
    tid, score = find_similar(db, title)
    return tid if score >= threshold else None


# ---------------------------------------------------------------------------
# adjudication
# ---------------------------------------------------------------------------

def adjudicate(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
               prop: dict[str, Any]) -> str:
    """Returns the resulting status.  Cheapest tier that can answer, answers."""
    from dispatch.arbiter import adjudicate_proposal

    _escalate_cfg = cfg
    ok, why = check_invariants(db, cfg, prop)
    if not ok:
        return _decide(db, prop, "rejected", "policy", why)

    mut = cfg.get("mutation", {})
    autonomy = mut.get("autonomy", "policy")
    kind = prop["kind"]

    if kind in ("escalate", "raise_blocker") and prop.get("urgency") == "high":
        return _escalate(db, prop, "agent asked for a human", cfg)

    if autonomy == "human":
        return _escalate(db, prop, "mutation autonomy is set to 'human'", cfg)

    if autonomy == "policy" and kind in mut.get("auto_accept_kinds", []):
        near, score = _suspected_duplicate(db, cfg, prop)
        if near:
            verdict = adjudicate_proposal(db, root, cfg, prop)
            action = verdict.get("action", "escalate")
            note = (f"possibly a duplicate of {near} ({score:.2f}) — "
                    + verdict.get("reason", ""))
            if action == "accept":
                return apply_proposal(db, root, cfg, workflows, prop, "arbiter", note)
            if action == "reject":
                return _decide(db, prop, "rejected", "arbiter", note)
            return _escalate(db, prop, note, cfg)
        return apply_proposal(db, root, cfg, workflows, prop, "policy",
                              "within policy: in budget, in depth, no cycle")

    if kind in mut.get("arbiter_kinds", []) or autonomy == "arbiter":
        verdict = adjudicate_proposal(db, root, cfg, prop)
        action = verdict.get("action", "escalate")
        note = verdict.get("reason", "")
        if action == "accept":
            return apply_proposal(db, root, cfg, workflows, prop, "arbiter", note)
        if action == "modify":
            prop = dict(prop)
            prop["payload"] = json.dumps(verdict.get("payload") or
                                         json.loads(prop["payload"]))
            return apply_proposal(db, root, cfg, workflows, prop, "arbiter",
                                  note, status="modified")
        if action == "reject":
            return _decide(db, prop, "rejected", "arbiter", note)
        return _escalate(db, prop, note or "arbiter deferred to you", cfg)

    return apply_proposal(db, root, cfg, workflows, prop, "policy", "default accept")


def _suspected_duplicate(db: DB, cfg: dict[str, Any], prop: dict[str, Any]):
    """Scores in the grey band between "obviously new" and "obviously the same"
    are exactly the ones a human notices later as two cards for one job."""
    if prop["kind"] not in ("add_task", "split"):
        return None, 0.0
    mut = cfg.get("mutation", {})
    review_at = float(mut.get("duplicate_review", 0.55))
    merge_at = float(mut.get("duplicate_similarity", 0.82))
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) \
        else prop["payload"]
    for spec in (payload.get("tasks") or [payload]):
        tid, score = find_similar(db, spec.get("title", ""), spec.get("brief", ""))
        if tid and review_at <= score < merge_at:
            return tid, score
    return None, 0.0


def _decide(db: DB, prop: dict[str, Any], status: str, tier: str,
            note: str) -> str:
    db.x("UPDATE proposals SET status=?, tier=?, decision=?, decided_by=?, decided_at=? "
         "WHERE id=?", (status, tier, note, tier, now(), prop["id"]))
    db.emit("proposal." + status, prop.get("from_task"), actor=tier,
            proposal_id=prop["id"], kind=prop["kind"], note=note[:400])
    return status


def _escalate(db: DB, prop: dict[str, Any], note: str,
              cfg: dict[str, Any] | None = None) -> str:
    from dispatch import board as B
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) else prop["payload"]
    tid = prop.get("from_task")
    if tid and B.get(db, tid):
        B.open_checkpoint(
            db, tid, kind="escalation", topic="proposal", cfg=cfg,
            question=f"Proposal from {tid}: {prop['kind']} — accept?",
            bundle={"proposal_id": prop["id"], "kind": prop["kind"],
                    "payload": payload, "rationale": prop.get("rationale"),
                    "why_escalated": note},
            actor="adjudicator")
    return _decide(db, prop, "escalated", "human", note)


def apply_proposal(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
                   prop: dict[str, Any], tier: str, note: str,
                   status: str = "accepted") -> str:
    from dispatch import board as B
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) else prop["payload"]
    kind, src = prop["kind"], prop.get("from_task")
    sim = float(cfg.get("mutation", {}).get("duplicate_similarity", 0.82))

    if kind in ("add_task", "split"):
        specs = payload.get("tasks") or [payload]
        made = []
        for s in specs:
            title = s.get("title") or "(untitled)"
            dup = find_duplicate(db, title, sim)
            if dup:
                db.emit("proposal.merged", src, proposal_id=prop["id"],
                        merged_into=dup, title=title)
                continue
            parent = s.get("parent_id") or payload.get("parent_id")
            if not parent:
                # `split` genuinely decomposes the originating card, so its
                # pieces are children. `add_task` is adjacent work discovered
                # in passing — that is a sibling, sharing the same budget pool.
                parent = src if kind == "split" else _parent_of(db, src) or src
            tid = B.create(
                db, cfg, workflows,
                title=title, brief=_with_staleness(db, s.get("brief", ""), prop),
                card_type=s.get("card_type") or _inherit_type(db, src),
                acceptance=s.get("acceptance") or [],
                parent_id=parent, tags=s.get("tags") or [],
                priority=int(s.get("priority", 50)),
                scope=s.get("scope"), provenance=f"agent:{src}",
                proposal_id=prop["id"])
            if s.get("depends_on"):
                for d in s["depends_on"]:
                    try:
                        B.link(db, d, tid)
                    except ValueError:
                        pass
            if kind == "split" and src:
                # the split-off work must land before the originating card can
                try:
                    B.link(db, tid, src)
                except ValueError:
                    pass
            if s.get("start", True):
                B.start_card(db, workflows, tid, actor=tier)
            made.append(tid)
        note = (note + f" — created {', '.join(made)}") if made else (note + " — all duplicates")

    elif kind == "add_dep":
        try:
            B.link(db, payload["src"], payload["dst"], payload.get("edge_kind", "finish_to_start"))
        except (ValueError, KeyError) as e:
            return _decide(db, prop, "rejected", tier, str(e))

    elif kind == "amend_brief":
        tid = payload.get("task_id") or src
        t = B.get(db, tid) if tid else None
        if t:
            extra = payload.get("append") or payload.get("brief") or ""
            B.update(db, tid, actor=tier,
                     brief=(t["brief"] + "\n\n## Added by " + (src or tier) + "\n" + extra))

    elif kind == "raise_blocker":
        tid = payload.get("task_id") or src
        if tid:
            B.update(db, tid, actor=tier, status=B.BLOCKED,
                     block_reason=payload.get("reason") or prop.get("rationale") or "blocked")

    elif kind == "request_gate":
        tid = payload.get("task_id") or src
        t = B.get(db, tid) if tid else None
        if t:
            B.update(db, tid, actor=tier, gates=[*list(t["gates"]), payload.get("gate")])

    elif kind == "cancel":
        tid = payload.get("task_id")
        if tid and tid != src:
            B.cancel(db, tid, actor=tier)
        else:
            return _decide(db, prop, "rejected", tier,
                           "an agent may not cancel its own card")

    return _decide(db, prop, status, tier, note)


def _with_staleness(db: DB, brief: str, prop: dict[str, Any]) -> str:
    """A proposal made from a worktree that has since fallen behind can ask for
    work that already landed. Say so on the card rather than letting an agent
    redo it."""
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) \
        else prop["payload"]
    behind = payload.get("base_behind")
    if not behind:
        return brief
    return (brief + f"\n\n> Proposed from a worktree {behind} commit(s) behind "
                    f"the base branch. Check the work has not already landed "
                    f"before starting.")


def _parent_of(db: DB, task_id: str | None) -> str | None:
    if not task_id:
        return None
    r = db.q1("SELECT parent_id FROM tasks WHERE id=?", (task_id,))
    return r["parent_id"] if r else None


def _inherit_type(db: DB, task_id: str | None) -> str:
    if not task_id:
        return "development"
    r = db.q1("SELECT card_type FROM tasks WHERE id=?", (task_id,))
    return r["card_type"] if r else "development"


# ---------------------------------------------------------------------------
# expansion alarm
# ---------------------------------------------------------------------------

def expansion_ratio(db: DB, cfg: dict[str, Any]) -> tuple[float, int, int]:
    """Agent-created ÷ completed, since you last acknowledged the alarm.

    Two corrections learned from a real session. Cards *you* create are not
    the board expanding itself — an operator making and cleaning up a mess
    counted as runaway agents and the alarm was right about the number and
    wrong about the cause. And the window used to be purely historical, so an
    acknowledged alarm re-fired forever; the only way through was to disable
    the guard.
    """
    w = int(cfg.get("containment", {}).get("expansion_ratio_window", 20))
    since = int(db.get_meta("expansion_ack_event", "0") or 0)
    rows = db.q("SELECT id, kind, actor FROM events "
                "WHERE kind IN ('task.created','task.done') AND id > ? "
                "ORDER BY id DESC LIMIT ?", (since, w * 3))
    created = sum(1 for r in rows
                  if r["kind"] == "task.created" and _by_agent(r["actor"]))
    done = sum(1 for r in rows if r["kind"] == "task.done")
    if created + done < w:
        return 0.0, created, done
    return (created / max(done, 1)), created, done


def _by_agent(actor: str | None) -> bool:
    a = (actor or "").lower()
    return a.startswith("agent:") or a in ("arbiter", "policy", "planner",
                                           "intent", "scheduler")


def acknowledge_expansion(db: DB, actor: str = "human") -> None:
    """Draw a line: the ratio is measured from here."""
    row = db.q1("SELECT MAX(id) AS m FROM events")
    db.set_meta("expansion_ack_event", str((row["m"] if row else 0) or 0))
    db.emit("expansion.acknowledged", actor=actor)

"""Card types and their pipelines.

A card type owns an ordered list of stages; each stage names the agent that
works it and the gates it must clear.  This is what makes "a development card
goes through the developer agent, then QA" a piece of repo configuration rather
than something an orchestrator has to remember.

Workflows are editable in the web UI and round-trip through JSON so a pipeline
you like can be imported into the next repo.
"""
from __future__ import annotations

import json
import time
from typing import Any

from dispatch.config import paths, stage_ids

DEFAULT_WORKFLOWS: dict[str, dict[str, Any]] = {
    "development": {
        "label": "Development",
        "color": "#1D6B58",
        "stages": [
            {"stage": "build",     "agent": "developer",
             "gates": ["tests_pass", "has_acceptance"]},
            {"stage": "qa",        "agent": "qa",
             "gates": ["tests_pass"]},
            {"stage": "review",    "agent": "reviewer", "gates": []},
            {"stage": "signoff",   "agent": "human",
             "gates": [], "auto_pass_if": "small_and_green"},
            {"stage": "integrate", "agent": "integrator",
             "gates": ["tests_pass"], "lock": "integration"},
        ],
    },
    "bugfix": {
        "label": "Bug fix",
        "color": "#A43829",
        "stages": [
            {"stage": "build", "agent": "developer",
             "gates": ["tests_pass", "has_acceptance"]},
            {"stage": "qa",    "agent": "qa", "gates": ["tests_pass"]},
            {"stage": "integrate", "agent": "integrator",
             "gates": ["tests_pass"], "lock": "integration"},
        ],
    },
    "chore": {
        "label": "Chore",
        "color": "#6B7A75",
        "stages": [
            {"stage": "build", "agent": "developer", "gates": ["tests_pass"]},
            {"stage": "integrate", "agent": "integrator",
             "gates": ["tests_pass"], "lock": "integration"},
        ],
    },
    # A human describes what they want; an agent reads the repo and proposes a
    # plan; the human approves it; the cards are created from it.
    "intent": {
        "label": "Direction",
        "color": "#2A5C7A",
        "merge": False,          # planning produces no code to land
        "stages": [
            {"stage": "spec", "agent": "planner", "gates": ["has_plan"]},
            {"stage": "signoff", "agent": "human", "gates": []},
        ],
    },
    "research": {
        "label": "Research",
        "color": "#9C6B10",
        "merge": False,
        "stages": [
            {"stage": "spec",    "agent": "spec", "gates": []},
            {"stage": "signoff", "agent": "human", "gates": []},
        ],
    },
}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def load(db) -> dict[str, dict[str, Any]]:
    rows = db.q("SELECT * FROM workflows ORDER BY card_type")
    if not rows:
        return json.loads(json.dumps(DEFAULT_WORKFLOWS))
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["card_type"]] = {
            "label": r["label"],
            "color": r["color"],
            "stages": json.loads(r["stages"]),
        }
    return out


def save(db, workflows: dict[str, dict[str, Any]], actor: str = "human") -> None:
    ts = time.time()
    existing = {r["card_type"] for r in db.q("SELECT card_type FROM workflows")}
    for ct, wf in workflows.items():
        db.x(
            "INSERT INTO workflows (card_type,label,color,stages,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(card_type) DO UPDATE SET "
            "label=excluded.label, color=excluded.color, stages=excluded.stages, "
            "updated_at=excluded.updated_at",
            (ct, wf.get("label", ct), wf.get("color", "#6B7A75"),
             json.dumps(wf.get("stages", [])), ts),
        )
    for gone in existing - set(workflows):
        db.x("DELETE FROM workflows WHERE card_type=?", (gone,))
    db.emit("workflows.saved", actor=actor, card_types=sorted(workflows))


def export_file(root: str, db) -> str:
    """Write workflows.json next to the board so it can be committed and
    imported into another repo."""
    p = paths(root)["workflows"]
    payload = {"version": 1, "card_types": load(db)}
    with open(p, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return p


def import_file(root: str, db, path: str | None = None) -> dict[str, Any]:
    p = path or paths(root)["workflows"]
    with open(p) as f:
        payload = json.load(f)
    wfs = payload.get("card_types", payload)
    save(db, wfs, actor="import")
    return wfs


# ---------------------------------------------------------------------------
# pipeline queries
# ---------------------------------------------------------------------------

def pipeline(workflows: dict[str, Any], card_type: str) -> list[dict[str, Any]]:
    wf = workflows.get(card_type)
    if not wf:
        return []
    return wf.get("stages", [])


def first_stage(workflows: dict[str, Any], card_type: str) -> dict[str, Any] | None:
    p = pipeline(workflows, card_type)
    return p[0] if p else None


def stage_entry(workflows: dict[str, Any], card_type: str,
                stage: str) -> dict[str, Any] | None:
    for e in pipeline(workflows, card_type):
        if e["stage"] == stage:
            return e
    return None


def next_stage(workflows: dict[str, Any], card_type: str,
               stage: str) -> dict[str, Any] | None:
    """The entry after `stage`, or None when the pipeline is exhausted."""
    p = pipeline(workflows, card_type)
    for i, e in enumerate(p):
        if e["stage"] == stage:
            return p[i + 1] if i + 1 < len(p) else None
    return None


def validate(workflows: dict[str, Any], cfg: dict[str, Any],
             agents: dict[str, Any]) -> list[str]:
    """Return human-readable problems.  The web editor shows these inline
    rather than refusing to save, so a half-finished pipeline is still saveable."""
    known_stages = set(stage_ids(cfg))
    known_agents = set(agents)
    problems: list[str] = []
    for ct, wf in workflows.items():
        stages = wf.get("stages", [])
        if not stages:
            problems.append(f"{ct}: pipeline is empty — cards of this type will never move")
        seen = set()
        order = {s: i for i, s in enumerate(stage_ids(cfg))}
        last = -1
        for e in stages:
            sid = e.get("stage")
            if sid not in known_stages:
                problems.append(f"{ct}: unknown stage '{sid}'")
                continue
            if sid in seen:
                problems.append(f"{ct}: stage '{sid}' appears twice")
            seen.add(sid)
            if order[sid] < last:
                problems.append(
                    f"{ct}: '{sid}' runs after a later column — cards would move backwards")
            last = order[sid]
            if e.get("agent") not in known_agents:
                problems.append(f"{ct}: stage '{sid}' names unknown agent '{e.get('agent')}'")
    return problems

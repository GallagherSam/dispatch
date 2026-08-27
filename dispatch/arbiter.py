"""The model as a subroutine.

The arbiter is a stateless LLM invocation with a small board slice and one
event.  It is never the loop, it holds no session, and it cannot stop anything
by returning.  Keeping it separate from workers is also what stops context
erosion sneaking back in through the side door.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from dispatch.db import DB


def _call(cfg: dict[str, Any], prompt: str) -> dict[str, Any] | None:
    a = cfg.get("arbiter", {})
    cmd = [p.replace("{model}", a.get("model") or "sonnet") for p in a.get("command", [])]
    if not cmd:
        return None
    from dispatch.config import agent_environment
    try:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             env=agent_environment(cfg),
                             timeout=a.get("timeout_s", 180))
    except Exception:
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return None
    try:
        env = json.loads(raw)
        body = env.get("result") if isinstance(env, dict) else raw
    except json.JSONDecodeError:
        body = raw
    return _extract_json(body or "")


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text
        text = text.lstrip("json").strip()
    start, depth = text.find("{"), 0
    if start < 0:
        return None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _board_slice(db: DB, task_id: str | None, limit: int = 25) -> str:
    rows = db.q("SELECT id,title,card_type,stage,status,parent_id FROM tasks "
                "WHERE status NOT IN ('done','cancelled') "
                "ORDER BY priority DESC, created_at LIMIT ?", (limit,))
    lines = [f"- {r['id']} [{r['card_type']}/{r['stage']}/{r['status']}] {r['title']}"
             + (f"  (child of {r['parent_id']})" if r["parent_id"] else "")
             for r in rows]
    return "\n".join(lines) or "(board is empty)"


def adjudicate_proposal(db: DB, root: str, cfg: dict[str, Any],
                        prop: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(prop["payload"]) if isinstance(prop["payload"], str) else prop["payload"]
    src = prop.get("from_task")
    origin = db.q1("SELECT title,brief FROM tasks WHERE id=?", (src,)) if src else None

    prompt = f"""You are the arbiter for an agent task board. A worker agent has proposed a
change to the board. Decide what happens to it. You are a subroutine: answer and stop.

## Originating card
{src or '(none)'} — {origin['title'] if origin else '(unknown)'}
{(origin['brief'][:1200] if origin else '')}

## Proposal
kind: {prop['kind']}
rationale: {prop.get('rationale') or '(none given)'}
confidence: {prop.get('confidence')}
payload:
{json.dumps(payload, indent=2)[:3000]}

## Current open cards
{_board_slice(db, src)}

## Your decision
Judge whether this proposal is real work that belongs on the board, whether it
duplicates something already there, and whether it is scoped correctly. Prefer
`reject` when the proposal restates existing work or is speculative. Prefer
`escalate` only when a human's intent is genuinely required — a scope change, a
contradiction with an existing card, or a cost the board should not absorb.

Reply with ONLY a JSON object:
{{"action": "accept" | "modify" | "reject" | "escalate",
  "reason": "one or two sentences",
  "payload": {{...}}   // required only for "modify": the corrected payload
}}"""

    got = _call(cfg, prompt)
    if not got or "action" not in got:
        return {"action": "escalate", "reason": "arbiter unavailable or unparseable"}
    db.emit("arbiter.decided", src, actor="arbiter", proposal_id=prop["id"],
            action=got.get("action"), reason=str(got.get("reason"))[:400])
    return got


def judge_acceptance(ctx: dict[str, Any]) -> Any:
    """Gate builtin: judge prose acceptance criteria against the actual diff."""
    from dispatch.gates import FAIL, PASS, Verdict

    task = ctx["task"]
    crit = task.get("acceptance") or []
    if not crit:
        return Verdict(PASS, "no criteria to judge")
    diff = (ctx.get("diff") or "")[:14000]
    summary = ctx.get("summary") or ""

    prompt = f"""You are a gate on an agent task board. Judge whether the work below meets
every acceptance criterion. Be strict: partial completion is a failure.

## Card
{task['id']} — {task['title']}

## Acceptance criteria
""" + "\n".join(f"{i+1}. {c}" for i, c in enumerate(crit)) + f"""

## Agent's summary
{summary[:2000]}

## Diff
```diff
{diff}
```

Reply with ONLY a JSON object:
{{"verdict": "pass" | "fail",
  "reason": "one sentence",
  "evidence": "if fail, exactly what is missing — this text is handed to the agent as its next instruction"}}"""

    got = _call(ctx["cfg"], prompt)
    if not got:
        return Verdict(PASS, "arbiter unavailable — not blocking on an unreachable judge")
    v = got.get("verdict", "pass")
    return Verdict(PASS if v == "pass" else FAIL, got.get("reason", ""),
                   evidence=got.get("evidence"))


def triage_failure(db: DB, cfg: dict[str, Any], task: dict[str, Any],
                   evidence: str) -> dict[str, Any]:
    """Called when a card exhausts its attempts. Decides retry-with-hint,
    decompose, or hand to a human."""
    prompt = f"""A card on an agent task board has failed {task['attempts']} times and is about
to be dead-lettered. Decide what to do with it.

## Card
{task['id']} — {task['title']}
stage: {task['stage']} · type: {task['card_type']}

{task['brief'][:2000]}

## Last failure evidence
```
{(evidence or '')[:5000]}
```

Reply with ONLY a JSON object:
{{"action": "retry" | "decompose" | "human",
  "reason": "one sentence",
  "hint": "if retry: what the agent should do differently",
  "tasks": [{{"title": "...", "brief": "..."}}]  // if decompose
}}"""
    got = _call(cfg, prompt)
    if not got:
        return {"action": "human", "reason": "arbiter unavailable"}
    db.emit("arbiter.triaged", task["id"], actor="arbiter",
            action=got.get("action"), reason=str(got.get("reason"))[:400])
    return got

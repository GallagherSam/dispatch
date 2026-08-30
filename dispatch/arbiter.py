"""The model as a subroutine.

The arbiter is a stateless LLM invocation with a small board slice and one
event.  It is never the loop, it holds no session, and it cannot stop anything
by returning.  Keeping it separate from workers is also what stops context
erosion sneaking back in through the side door.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any, NamedTuple

from dispatch.db import DB, new_id

#: Why a call produced no usable answer. The distinction matters: an arbiter
#: nobody configured is a standing condition, and no amount of waiting fixes
#: it, while a crashed subprocess is worth retrying. Collapsing the two is how
#: "not blocking on an unreachable judge" came to mean "passing the only gate
#: that costs money whenever the network hiccups".
OK, UNCONFIGURED, UNREACHABLE, UNREADABLE = (
    "ok", "unconfigured", "unreachable", "unreadable")

#: How many times a card will wait for an unreachable arbiter before asking a
#: human instead. Bounded on purpose — an unbounded defer is a stall wearing a
#: retry's clothes.
_DEFER_LIMIT = 3


class Call(NamedTuple):
    data: dict[str, Any] | None
    outcome: str
    usd: float = 0.0
    detail: str = ""

    def __bool__(self) -> bool:            # `if got:` keeps reading naturally
        return self.data is not None


def _record(db: DB | None, purpose: str, task_id: str | None,
            model: str, call: Call, seconds: float) -> None:
    """Arbiter spend is real money and used to be discarded.

    The CLI reports `total_cost_usd` on every call and this threw it away with
    the rest of the envelope, so board spend, subtree budgets and the
    `budget_remaining` gate were all blind to every judgment, adjudication and
    triage the board ever made. A separate table rather than `runs`: a run
    means an agent worked a card, `runs.task_id` is NOT NULL, and adjudicating
    a proposal that came from no card still costs money.
    """
    if db is None:
        return
    try:
        db.x("INSERT INTO arbiter_calls (id,task_id,purpose,model,outcome,usd,"
             "duration_s,created_at) VALUES (?,?,?,?,?,?,?,?)",
             (new_id("ac"), task_id, purpose, model, call.outcome,
              call.usd, seconds, time.time()))
    except Exception:
        # never let bookkeeping break an adjudication
        pass


def _unreachable_since(db: DB | None, task_id: str | None) -> int:
    """Consecutive unreachable judgments for this card, most recent first.

    Consecutive, not total: an arbiter that failed twice last week and works
    now should not push a card straight to a human.
    """
    if db is None or not task_id:
        return 0
    rows = db.q("SELECT outcome FROM arbiter_calls WHERE task_id=? AND "
                "purpose='judge_acceptance' ORDER BY created_at DESC LIMIT ?",
                (task_id, _DEFER_LIMIT + 1))
    n = 0
    for r in rows:
        if r["outcome"] != UNREACHABLE:
            break
        n += 1
    return n


def _call(cfg: dict[str, Any], prompt: str, *, db: DB | None = None,
          purpose: str = "arbiter", task_id: str | None = None) -> Call:
    a = cfg.get("arbiter", {})
    model = a.get("model") or "sonnet"
    cmd = [p.replace("{model}", model) for p in a.get("command", [])]
    started = time.time()

    def done(call: Call) -> Call:
        _record(db, purpose, task_id, model, call, time.time() - started)
        return call

    if not cmd:
        return done(Call(None, UNCONFIGURED, detail="no arbiter command configured"))
    from dispatch.config import agent_environment
    try:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             env=agent_environment(cfg),
                             timeout=a.get("timeout_s", 180))
    except Exception as e:
        return done(Call(None, UNREACHABLE, detail=f"{type(e).__name__}: {e}"))

    raw = (out.stdout or "").strip()
    if not raw:
        return done(Call(None, UNREACHABLE,
                         detail=f"no output (exit {out.returncode})"))

    usd, body = 0.0, raw
    try:
        env = json.loads(raw)
        if isinstance(env, dict):
            usd = float(env.get("total_cost_usd") or 0.0)
            body = env.get("result") or ""
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    data = _extract_json(body or "")
    if data is None:
        return done(Call(None, UNREADABLE, usd,
                         detail=f"no JSON object in reply: {(body or '')[:200]}"))
    return done(Call(data, OK, usd))


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model's reply.

    This counted braces once, which cannot work: the arbiter's own prompt asks
    for `evidence` describing what is missing, and evidence about code says
    things like "missing } in the config block". One unbalanced brace inside a
    string ended the object early, the parse returned None, and — because an
    unreachable arbiter passed — the card sailed through the acceptance gate
    on a reply nobody could read.

    `raw_decode` is the same parser `json.loads` uses, so strings, escapes and
    nesting are its problem rather than ours. It is pointed at each `{` in turn
    because models like to open with a sentence.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text
        # only a language tag, and only its own line — `lstrip("json")` would
        # eat the leading letters of a body that starts without a fence tag
        first, _, rest = text.partition("\n")
        if first.strip().lower() in ("json", ""):
            text = rest or text
        text = text.strip()

    decoder = json.JSONDecoder()
    at = text.find("{")
    while at >= 0:
        try:
            value, _ = decoder.raw_decode(text[at:])
        except json.JSONDecodeError:
            at = text.find("{", at + 1)
            continue
        return value if isinstance(value, dict) else None
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

    got = _call(cfg, prompt, db=db, purpose="adjudicate", task_id=src)
    if not got or "action" not in got.data:
        return {"action": "escalate", "reason": "arbiter unavailable or unparseable"}
    db.emit("arbiter.decided", src, actor="arbiter", proposal_id=prop["id"],
            action=got.data.get("action"),
            reason=str(got.data.get("reason"))[:400])
    return got.data


def judge_acceptance(ctx: dict[str, Any]) -> Any:
    """Gate builtin: judge prose acceptance criteria against the actual diff."""
    from dispatch.gates import DEFER, ESCALATE, FAIL, PASS, Verdict

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

    got = _call(ctx["cfg"], prompt, db=ctx.get("db"),
                purpose="judge_acceptance", task_id=task.get("id"))
    if not got:
        # This passed on any failure, which made the one gate that costs money
        # also the one gate a network blip could walk straight through. It
        # cannot simply defer instead: with no arbiter configured, or with a
        # model that keeps replying in prose, the card would requeue forever
        # with no attempt spent and nothing to look at — a silent stall, which
        # is the failure mode this board has been bitten by before.
        if got.outcome == UNCONFIGURED:
            return Verdict(ESCALATE, "arbiter_judges is on this stage but no arbiter "
                                "is configured", evidence=got.detail)
        # The retry budget cannot come from `task["attempts"]`: a defer
        # deliberately spends no attempt, so that counter never moves and the
        # card would defer forever. Count the failed calls themselves — which
        # is exactly what recording them bought.
        tries = _unreachable_since(ctx.get("db"), task.get("id"))
        if got.outcome == UNREACHABLE and tries < _DEFER_LIMIT:
            return Verdict(DEFER,
                           f"arbiter unreachable, retrying "
                           f"({tries}/{_DEFER_LIMIT})",
                           evidence=got.detail, retry_after_s=60)
        return Verdict(ESCALATE,
                  f"arbiter {got.outcome} — judge this card yourself",
                  evidence=got.detail)
    v = got.data.get("verdict", "pass")
    return Verdict(PASS if v == "pass" else FAIL, got.data.get("reason", ""),
                   evidence=got.data.get("evidence"))


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
    got = _call(cfg, prompt, db=db, purpose="triage", task_id=task["id"])
    if not got:
        # handing it to a human is already the conservative answer here
        return {"action": "human", "reason": f"arbiter {got.outcome}"}
    db.emit("arbiter.triaged", task["id"], actor="arbiter",
            action=got.data.get("action"),
            reason=str(got.data.get("reason"))[:400])
    return got.data

"""Gates: the only thing permitted to declare work complete.

Four verdicts, not a boolean:

    pass      proceed
    defer     conditions not met but will be — requeue, no attempt consumed
    fail      the work is wrong — return it with evidence, attempt++
    escalate  policy or budget breach — open a human checkpoint

`defer` vs `fail` is the distinction most systems collapse, and separating them
is what lets a quota gate hold a task for six hours without poisoning it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

PASS, DEFER, FAIL, ESCALATE = "pass", "defer", "fail", "escalate"


class Verdict:
    __slots__ = ("evidence", "gate", "reason", "retry_after_s", "verdict")

    def __init__(self, verdict: str, reason: str = "", retry_after_s: float = 0.0,
                 evidence: str | None = None, gate: str = ""):
        self.verdict = verdict
        self.reason = reason
        self.retry_after_s = retry_after_s
        self.evidence = evidence
        self.gate = gate

    def __repr__(self) -> str:
        return f"<{self.gate}:{self.verdict} {self.reason!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "verdict": self.verdict, "reason": self.reason,
                "retry_after_s": self.retry_after_s, "evidence": self.evidence}


def _p(reason: str = "", gate: str = "") -> Verdict:
    return Verdict(PASS, reason, gate=gate)


def _glob_re(pattern: str) -> re.Pattern:
    """Path-aware glob. `fnmatch` lets `*` cross a slash, which quietly makes
    `src/*` match `src/a/b/c.py` — not what anyone means when they write a
    scope. Here `*` stops at `/` and `**` does not."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif c == "*" and pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, patterns) -> bool:
    return any(_glob_re(g).match(path) for g in patterns)


def parse_spec(spec: Any) -> dict[str, Any]:
    """Accept either `"tests_pass"` / `"quota_above:30"` shorthand or a full
    object.  Shorthand keeps hand-edited workflow JSON readable."""
    if isinstance(spec, dict):
        d = dict(spec)
        d.setdefault("args", [])
        return d
    name, _, rest = str(spec).partition(":")
    args = [a for a in rest.split(",") if a != ""] if rest else []
    return {"gate": name.strip(), "args": args}


# ---------------------------------------------------------------------------
# built-ins
# ---------------------------------------------------------------------------

def _g_concurrency(ctx, args) -> Verdict:
    limit = int(args[0]) if args else ctx["cfg"]["scheduler"]["max_concurrent"]
    running = ctx["db"].q1("SELECT COUNT(*) c FROM leases")["c"]
    if running >= limit:
        return Verdict(DEFER, f"{running}/{limit} agents already running",
                       retry_after_s=10)
    return _p()


def _g_wip_limit(ctx, args) -> Verdict:
    task = ctx["task"]
    from dispatch.config import stage_def
    stage = args[0] if args else task["stage"]
    limit = int(args[1]) if len(args) > 1 else int(stage_def(ctx["cfg"], stage).get("wip") or 0)
    if limit <= 0:
        return _p()
    n = ctx["db"].q1(
        "SELECT COUNT(*) c FROM tasks t JOIN leases l ON l.task_id=t.id WHERE t.stage=?",
        (stage,))["c"]
    if n >= limit:
        return Verdict(DEFER, f"WIP limit on {stage}: {n}/{limit}", retry_after_s=15)
    return _p()


def _g_time_window(ctx, args) -> Verdict:
    if len(args) < 2:
        return _p()
    start, end = args[0], args[1]
    nowt = time.strftime("%H:%M")
    inside = (start <= nowt <= end) if start <= end else (nowt >= start or nowt <= end)
    if not inside:
        return Verdict(DEFER, f"outside dispatch window {start}-{end}", retry_after_s=600)
    return _p()


def _g_quota_above(ctx, args) -> Verdict:
    """Reads `.dispatch/gates/quota.sh`, which prints remaining percent on
    stdout.  Absent or non-zero-exit means "unknown", and unknown passes —
    a gate you have not taught should not silently stop the board."""
    threshold = float(args[0]) if args else 15.0
    script = os.path.join(ctx["paths"]["gates"], "quota.sh")
    if not os.path.exists(script):
        return _p("no quota probe installed")
    try:
        out = subprocess.run([script], capture_output=True, text=True, timeout=20)
    except Exception as e:
        return _p(f"quota probe failed: {e}")
    if out.returncode != 0:
        return _p("quota probe returned non-zero")
    try:
        remaining = float(out.stdout.strip().rstrip("%"))
    except ValueError:
        return _p("quota probe printed nothing parseable")
    if remaining < threshold:
        return Verdict(DEFER, f"quota at {remaining:.0f}% (need {threshold:.0f}%)",
                       retry_after_s=1800)
    return _p(f"quota {remaining:.0f}%")


def _g_budget_remaining(ctx, args) -> Verdict:
    """The per-subtree ceiling is not a board ceiling: ten cards can each stay
    under it and the total can be four times what you thought you agreed to."""
    from dispatch import board as B
    task, db, cfg = ctx["task"], ctx["db"], ctx["cfg"]

    total_cap = cfg.get("containment", {}).get("total_budget_usd")
    if total_cap is not None:
        total = B.spend(db)["usd"]
        if total >= float(total_cap):
            return Verdict(ESCALATE,
                           f"the board has spent ${total:.2f} of its "
                           f"${float(total_cap):.2f} ceiling",
                           evidence="Raise containment.total_budget_usd, or set "
                                    "it to null to remove the ceiling.")

    cap, spent = B.subtree_budget(db, cfg, task["id"])
    limit = cap.get("usd")
    if limit is None:
        return _p()
    if spent.get("usd", 0.0) >= limit:
        return Verdict(ESCALATE,
                       f"subtree budget exhausted: ${spent.get('usd', 0):.2f} of ${limit:.2f}")
    return _p(f"${spent.get('usd', 0):.2f}/${limit:.2f}")


def _g_mutex_free(ctx, args) -> Verdict:
    db, task = ctx["db"], ctx["task"]
    rows = db.q(
        "SELECT src,dst FROM edges WHERE kind='mutex' AND (src=? OR dst=?)",
        (task["id"], task["id"]))
    others = {r["src"] if r["dst"] == task["id"] else r["dst"] for r in rows}
    if not others:
        return _p()
    qmarks = ",".join("?" * len(others))
    busy = db.q(f"SELECT task_id FROM leases WHERE task_id IN ({qmarks})", tuple(others))
    if busy:
        return Verdict(DEFER, f"mutex held by {busy[0]['task_id']}", retry_after_s=20)
    return _p()


def _g_has_acceptance(ctx, args) -> Verdict:
    """Runs before dispatch, not after. A card nobody can check is not work an
    agent should start — and escalating costs one human minute instead of three
    agent runs that learn nothing."""
    if not ctx["task"].get("acceptance"):
        return Verdict(
            ESCALATE,
            "no acceptance criteria — a gate can only be as good as the check behind it",
            evidence="Add at least one acceptance check to this card before it runs. "
                     "If the work cannot be checked, it cannot be gated.")
    return _p()


def _g_has_plan(ctx, args) -> Verdict:
    """A plan a human is asked to approve has to be reviewable, and the cards
    it becomes have to be workable. Catch a shapeless one before it reaches
    someone's attention."""
    import json as _json
    raw = ctx["task"].get("plan")
    if not raw:
        return Verdict(FAIL, "no plan was produced",
                       evidence="Write the plan JSON to $DISPATCH_RESULT under "
                                "a top-level `plan` key. See the planner brief.")
    try:
        plan = _json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return Verdict(FAIL, "the plan is not valid JSON")

    cards = plan.get("cards") or []
    if not cards:
        return Verdict(
            ESCALATE, "the planner produced no cards",
            evidence=(plan.get("summary") or "")[:2000] +
                     "\n\nUsually this means the request was too vague to plan. "
                     "Amend the direction with more detail, or reject it.")
    problems = []
    refs = {c.get("ref") for c in cards}
    for i, c in enumerate(cards, 1):
        where = c.get("ref") or f"card {i}"
        if not c.get("title"):
            problems.append(f"{where}: no title")
        if not c.get("brief"):
            problems.append(f"{where}: no brief — the agent gets nothing to work from")
        if not c.get("acceptance"):
            problems.append(f"{where}: no acceptance criteria, so no gate can judge it")
        for dep in c.get("depends_on") or []:
            if dep not in refs:
                problems.append(f"{where}: depends on '{dep}', which is not in the plan")
    if problems:
        return Verdict(FAIL, f"{len(problems)} problem(s) with the plan",
                       evidence="\n".join(f"- {p}" for p in problems[:20]))
    return _p(f"{len(cards)} card(s), all checkable")


def _g_diff_scope(ctx, args) -> Verdict:
    """Each card may declare the globs it is allowed to touch.  This turns
    'I hope these four agents don't collide' into a checked invariant."""
    task = ctx["task"]
    scope = list(args) or task.get("workspace", {}).get("scope") or []
    if not scope:
        return _p("no scope declared")
    files = ctx.get("changed_files") or []
    already = set((task.get("workspace") or {}).get("pre_existing") or [])
    stray = [f for f in files
             if f not in already and not glob_match(f, scope)]
    if stray:
        return Verdict(
            FAIL, f"{len(stray)} file(s) outside declared scope",
            evidence="Out of scope:\n  " + "\n  ".join(stray[:25]) +
                     "\n\nAllowed globs: " + ", ".join(scope))
    note = f"{len(files)} file(s), all in scope"
    if already:
        note += f" ({len(already)} were already dirty and not this card's)"
    return _p(note)


_SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
    (r"sk-[A-Za-z0-9]{32,}", "generic secret key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub token"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    (r"(?i)(password|passwd|secret|token)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']", "hardcoded credential"),
]


def _g_no_stray_writes(ctx, args) -> Verdict:
    """An agent that writes to an absolute path lands its work in the main repo
    instead of its branch. The card then merges nothing, and the dirtied tree
    blocks every other card's merge — silently at both ends, because the agent
    reports success. This is the thing that makes it loud."""
    stray = ctx.get("stray_writes") or []
    if not stray:
        return _p()
    wt = (ctx["task"].get("workspace") or {}).get("worktree") or "your worktree"
    listed = "\n  ".join(stray[:25])
    return Verdict(
        FAIL, f"{len(stray)} file(s) were written outside the worktree",
        evidence=(
            "These paths changed in the main repository during this run:\n  "
            + listed +
            f"\n\nWork there is NOT on this card's branch, so it will not "
            f"land, and it blocks every other card from merging. Redo the "
            f"change using paths relative to your working directory "
            f"({wt}). Never write to an absolute path outside it."))


def _g_no_secrets(ctx, args) -> Verdict:
    diff = ctx.get("diff") or ""
    hits: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat, label in _SECRET_PATTERNS:
            if re.search(pat, line):
                hits.append(f"{label}: {line[:110]}")
                break
    if hits:
        return Verdict(ESCALATE, f"{len(hits)} possible secret(s) in the diff",
                       evidence="\n".join(hits[:15]))
    return _p()


def _run_cmd_gate(ctx, args, label: str, default_key: str) -> Verdict:
    cmd = " ".join(args) if args else ctx["cfg"].get("commands", {}).get(default_key)
    if not cmd:
        return _p(f"no {label} command configured")
    cwd = ctx.get("cwd") or ctx["root"]
    try:
        out = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                             text=True, timeout=ctx["cfg"].get("commands", {})
                             .get("timeout_s", 900))
    except subprocess.TimeoutExpired:
        return Verdict(FAIL, f"{label} timed out", evidence=f"$ {cmd}\n(timed out)")
    if out.returncode != 0:
        tail = (out.stdout + "\n" + out.stderr).strip()[-4000:]
        return Verdict(FAIL, f"{label} failed (exit {out.returncode})",
                       evidence=f"$ {cmd}\n{tail}")
    return _p(f"{label} clean")


def _g_tests_pass(ctx, args) -> Verdict:
    return _run_cmd_gate(ctx, args, "tests", "test")


def _g_lint_clean(ctx, args) -> Verdict:
    return _run_cmd_gate(ctx, args, "lint", "lint")


def _g_build_ok(ctx, args) -> Verdict:
    return _run_cmd_gate(ctx, args, "build", "build")


def _g_arbiter_judges(ctx, args) -> Verdict:
    """Fuzzy acceptance criteria, judged by a model.  Deliberately the only
    builtin that costs money."""
    from dispatch.arbiter import judge_acceptance
    return judge_acceptance(ctx)


BUILTINS = {
    "concurrency": _g_concurrency,
    "wip_limit": _g_wip_limit,
    "time_window": _g_time_window,
    "quota_above": _g_quota_above,
    "budget_remaining": _g_budget_remaining,
    "mutex_free": _g_mutex_free,
    "has_acceptance": _g_has_acceptance,
    "has_plan": _g_has_plan,
    "diff_scope": _g_diff_scope,
    "no_secrets": _g_no_secrets,
    "no_stray_writes": _g_no_stray_writes,
    "tests_pass": _g_tests_pass,
    "lint_clean": _g_lint_clean,
    "build_ok": _g_build_ok,
    "arbiter_judges": _g_arbiter_judges,
}

# Which hook each builtin belongs to when a workflow lists it without one.
DEFAULT_HOOK = {
    "concurrency": "pre_dispatch", "wip_limit": "pre_dispatch",
    "time_window": "pre_dispatch", "quota_above": "pre_dispatch",
    "budget_remaining": "pre_dispatch", "mutex_free": "pre_dispatch",
    "has_acceptance": "pre_dispatch", "has_plan": "pre_complete",
    "diff_scope": "pre_complete",
    "no_secrets": "pre_complete", "no_stray_writes": "pre_complete",
    "tests_pass": "pre_complete",
    "lint_clean": "pre_complete", "build_ok": "pre_complete",
    "arbiter_judges": "pre_complete",
}


# ---------------------------------------------------------------------------
# external gates
# ---------------------------------------------------------------------------

def run_external(ctx, name: str, args: list[str]) -> Verdict:
    """A gate is an executable: task JSON on stdin, a verdict object on stdout.

    That keeps gates scriptable, testable in isolation, and writable at 11pm
    without touching the scheduler."""
    gates_dir = ctx["paths"]["gates"]
    for cand in (name, name + ".sh", name + ".py"):
        path = os.path.join(gates_dir, cand)
        if os.path.exists(path) and os.access(path, os.X_OK):
            break
    else:
        return Verdict(FAIL, f"unknown gate '{name}'",
                       evidence="Not a builtin and no executable of that name in "
                                f"{gates_dir}")
    env = dict(os.environ)
    env.update({
        "DISPATCH_ROOT": ctx["root"],
        "DISPATCH_TASK_ID": ctx["task"]["id"],
        "DISPATCH_STAGE": ctx["task"]["stage"],
        "DISPATCH_HOOK": ctx.get("hook", ""),
        "DISPATCH_WORKTREE": ctx.get("cwd") or ctx["root"],
        "DISPATCH_DIFF_FILE": ctx.get("diff_file") or "",
        "DISPATCH_BOARD_DB": ctx["paths"]["db"],
    })
    try:
        out = subprocess.run([path, *list(args)], input=json.dumps(ctx["task"], default=str),
                             capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return Verdict(FAIL, f"gate '{name}' timed out")
    body = out.stdout.strip()
    if body:
        try:
            d = json.loads(body)
            return Verdict(d.get("verdict", PASS if out.returncode == 0 else FAIL),
                           d.get("reason", ""), float(d.get("retry_after_s") or 0),
                           d.get("evidence"))
        except json.JSONDecodeError:
            pass
    # No JSON?  Fall back to exit code, which makes a one-line shell gate valid.
    if out.returncode == 0:
        return _p(body[:200])
    return Verdict(FAIL, f"gate '{name}' exited {out.returncode}",
                   evidence=(out.stdout + out.stderr)[-3000:])


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

def collect(cfg: dict[str, Any], workflows: dict[str, Any], task: dict[str, Any],
            hook: str) -> list[dict[str, Any]]:
    """Global gates, then the stage's gates, then task-level overrides."""
    from dispatch.workflows import stage_entry
    specs: list[Any] = list(cfg.get("global_gates", {}).get(hook, []))
    entry = stage_entry(workflows, task["card_type"], task["stage"]) or {}
    specs += list(entry.get("gates", []))
    specs += list(task.get("gates", []))

    out, seen = [], set()
    for s in specs:
        d = parse_spec(s)
        name = d["gate"]
        h = d.get("hook") or DEFAULT_HOOK.get(name, hook)
        if h != hook:
            continue
        key = (name, tuple(d.get("args", [])))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def evaluate(ctx: dict[str, Any], hook: str) -> tuple[Verdict, list[Verdict]]:
    """Run every gate for `hook`.  Returns the governing verdict plus the trail.

    Precedence when several gates disagree: escalate > fail > defer > pass.  The
    most restrictive answer wins, and the reason travels with it.
    """
    ctx = dict(ctx)
    ctx["hook"] = hook
    specs = collect(ctx["cfg"], ctx["workflows"], ctx["task"], hook)
    results: list[Verdict] = []
    for spec in specs:
        name, args = spec["gate"], [str(a) for a in spec.get("args", [])]
        fn = BUILTINS.get(name)
        try:
            v = fn(ctx, args) if fn else run_external(ctx, name, args)
        except Exception as e:  # a broken gate must not take the scheduler down
            v = Verdict(FAIL, f"gate '{name}' raised {type(e).__name__}: {e}")
        v.gate = name
        results.append(v)
        db = ctx.get("db")
        if db is not None:
            db.x("INSERT INTO gate_runs (task_id,run_id,gate,hook,verdict,reason,"
                 "retry_after_s,evidence,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ctx["task"]["id"], ctx.get("run_id"), name, hook, v.verdict,
                  v.reason, v.retry_after_s, v.evidence, time.time()))

    rank = {PASS: 0, DEFER: 1, FAIL: 2, ESCALATE: 3}
    governing = Verdict(PASS, "no gates" if not results else "all gates clear")
    for v in results:
        if rank[v.verdict] > rank[governing.verdict]:
            governing = v
    if governing.verdict == DEFER:
        governing.retry_after_s = max(
            (v.retry_after_s for v in results if v.verdict == DEFER), default=30) or 30
    return governing, results

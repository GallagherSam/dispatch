#!/usr/bin/env python3
"""Build the board in the README screenshot, without running an agent.

The shot goes stale the moment a chip or a column changes, and a screenshot
nobody can regenerate is one nobody updates. This writes the same board every
time: no model is called, no worktree is created, and the runs are inserted
directly with plausible costs.

    python3 docs/images/seed_demo_board.py /tmp/demo
    cd /tmp/demo && dispatch serve --port 7994

Then capture at 1854 CSS px wide, in both colour schemes.
"""
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dispatch import board as B
from dispatch import workflows as W
from dispatch.config import load_config, paths
from dispatch.db import DB

NOW = time.time()


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True,
                   capture_output=True)


def build_repo(root):
    os.makedirs(os.path.join(root, "src/api"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    with open(os.path.join(root, "src/api/app.py"), "w") as f:
        f.write("def handler():\n    pass\n")
    with open(os.path.join(root, "tests/test_api.py"), "w") as f:
        f.write("def test_ok():\n    assert True\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "demo@example.com")
    git(root, "config", "user.name", "demo")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    from dispatch.cli import main as cli
    cli(["init", root, "--no-verify"])


def main(root):
    os.makedirs(root, exist_ok=True)
    build_repo(root)
    db, cfg = DB(paths(root)["db"]), load_config(root)
    wfs = W.load(db)

    def card(stage, status, **kw):
        tid = B.create(db, cfg, wfs, **kw)
        B.update(db, tid, stage=stage, status=status)
        return tid

    def run(tid, stage, agent, usd, secs, model=None, summary="",
            status="done"):
        db.x("INSERT INTO runs (id,task_id,stage,agent_type,model,attempt,"
             "status,exit_code,summary,usd,duration_s,started_at,finished_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("r_" + uuid.uuid4().hex[:8], tid, stage, agent, model, 1, status,
              0, summary, usd, secs, NOW - secs - 60, NOW - 60))

    def lease(tid, stage, pid):
        db.x("INSERT INTO leases (task_id,run_id,pid,stage,heartbeat_at,"
             "expires_at) VALUES (?,?,?,?,?,?)",
             (tid, "r_" + uuid.uuid4().hex[:6], pid, stage, NOW, NOW + 900))

    a = card("review", "running",
             title="Token bucket limiter, per API key",
             brief="Per API key, not per IP — customers behind shared NAT.",
             acceptance=["python3 -m pytest tests/test_limiter.py passes",
                         "p99 latency unchanged on existing endpoints"],
             tags=["api", "core"], scope=["src/api/**"], priority=70)
    lease(a, "review", 4821)
    run(a, "build", "developer", 2.41, 384, summary="token bucket + redis")
    run(a, "qa", "qa", 0.88, 121, summary="18 cases, 2 edge cases added")

    b = card("build", "running",
             title="Redis connection pool exhaustion under burst",
             brief="Pool saturates at ~400 rps and requests queue behind it.",
             acceptance=["python3 -m pytest tests/test_pool.py passes"],
             card_type="bugfix", tags=["api", "perf"], priority=85,
             model="opus")
    lease(b, "build", 4833)
    run(b, "build", "developer", 1.12, 203, model="opus",
        summary="reproduced under load", status="failed")

    s = card("spec", "running",
             title="Define the 429 contract for SDK clients",
             brief="What the SDKs may assume: headers, units, retry semantics.",
             acceptance=["docs/api/429-contract.md exists with an example"],
             tags=["api", "spec"], model="opus")
    lease(s, "spec", 4855)

    c = card("backlog", "blocked",
             title="Rate limit headers on 429 responses",
             brief="Retry-After and X-RateLimit-* so clients can back off.",
             acceptance=["python3 -m pytest tests/test_headers.py passes"],
             tags=["api"], depends_on=[a])

    card("backlog", "queued",
         title="Document the limits in the public API reference",
         brief="Per-key quotas, burst allowance, and the 429 contract.",
         acceptance=["docs/api/rate-limits.md exists and links from the index"],
         card_type="chore", tags=["docs"], depends_on=[a], model="haiku")

    card("backlog", "blocked",
         title="Retire the legacy IP-based throttle",
         brief="Dead once per-key limits land. Remove config and middleware.",
         acceptance=["python3 -m pytest passes",
                     "no references to IPThrottle remain"],
         card_type="chore", tags=["cleanup"], depends_on=[a, c])

    card("build", "queued",
         title="Burst allowance per plan tier",
         brief="Free, pro and enterprise get different burst ceilings.",
         acceptance=["python3 -m pytest tests/test_tiers.py passes"],
         tags=["api", "billing"], priority=55)

    e = card("qa", "queued",
             title="Load test harness for the limiter",
             brief="Reproducible burst profile so p99 claims are checkable.",
             acceptance=["scripts/loadtest.py runs and reports p50/p99"],
             tags=["perf", "tooling"])
    run(e, "build", "developer", 1.74, 268, summary="harness + burst profile")

    t4 = card("qa", "queued",
              title="Contract tests against the published SDK",
              brief="Pin the 429 behaviour the SDKs depend on.",
              acceptance=["python3 -m pytest tests/test_contract.py passes"],
              tags=["api", "tests"])
    run(t4, "build", "developer", 1.19, 176, summary="contract suite")

    f = card("signoff", "blocked",
             title="Per-key quota admin endpoint",
             brief="Raise or lower a customer's ceiling without a deploy.",
             acceptance=["python3 -m pytest tests/test_admin.py passes"],
             tags=["api", "admin"], priority=40)
    run(f, "build", "developer", 2.05, 331, summary="endpoint + authz")
    run(f, "qa", "qa", 0.71, 96, summary="authz cases pass")
    run(f, "review", "reviewer", 1.33, 142, model="opus",
        summary="wants an audit log entry per change")
    B.open_checkpoint(
        db, f,
        "Reviewer wants an audit log entry per quota change. In scope here, "
        "or a follow-up card?",
        bundle={"reason": "review found no audit trail on a privileged "
                          "endpoint", "stage": "signoff"},
        kind="signoff", topic="review")

    i = card("integrate", "merging",
             title="Limiter metrics on the ops dashboard",
             brief="Allowed/throttled per key, and pool saturation.",
             acceptance=["dashboards/limiter.json renders with live data"],
             tags=["observability"])
    run(i, "build", "developer", 1.51, 224, summary="panels + queries")
    run(i, "qa", "qa", 0.54, 81, summary="renders against staging")
    run(i, "review", "reviewer", 0.97, 103, summary="approved")

    h = card("done", "done",
             title="Redis eviction policy audit",
             brief="allkeys-lru silently drops counters under memory pressure.",
             acceptance=["policy documented and set to noeviction"],
             card_type="bugfix", tags=["infra"], priority=60)
    run(h, "build", "developer", 0.94, 118, summary="policy set")
    run(h, "review", "reviewer", 0.62, 74, summary="approved")

    t3 = card("done", "done",
              title="Extract limiter config into settings",
              brief="Hard-coded ceilings moved behind the settings object.",
              acceptance=["python3 -m pytest passes",
                          "no literals left in the limiter"],
              card_type="chore", tags=["cleanup"])
    run(t3, "build", "developer", 0.68, 94, summary="config extracted")
    run(t3, "review", "reviewer", 0.41, 58, summary="approved")

    # The header reads "scheduler down" without a live pid, which is not what
    # the screenshot is meant to show. Any living process answers the check.
    # Detached, with its streams closed: inheriting this script's stdout keeps
    # the calling shell waiting on a process that sleeps for an hour.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(3600)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    with open(paths(root)["pid"], "w") as fh:
        fh.write(str(proc.pid))

    stats = db.q1("SELECT ROUND(SUM(usd),2) u, COUNT(*) n FROM runs")
    print(f"{len(B.all_tasks(db))} cards, ${stats['u']} over {stats['n']} runs")
    print(f"stand-in scheduler pid {proc.pid} — kill it when you are done")
    print(f"cd {root} && dispatch serve --port 7994")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dispatch-demo")

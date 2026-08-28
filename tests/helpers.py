"""Test fixtures: a throwaway git repo with a board, and a fake agent.

The fake agent is driven by `.dispatch/fake_agent.json`, so a test can say
"at the qa stage, write this file and exit 1" without touching the runner.
Tests exercise the real runner path — worktrees, commits, diffs, gates — with
only the model call replaced.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dispatch import board as B
from dispatch import workflows as W
from dispatch.cli import main as cli_main
from dispatch.config import load_config, paths, save_config
from dispatch.db import DB

FAKE_AGENT = '''\
import json, os, pathlib, sys, time
sys.stdin.read()
root = os.environ["DISPATCH_ROOT"]
stage = os.environ.get("DISPATCH_STAGE", "?")
ctl_path = os.path.join(root, ".dispatch", "fake_agent.json")
ctl = json.load(open(ctl_path)) if os.path.exists(ctl_path) else {}
plan = ctl.get(stage, ctl.get("*", {}))
if plan.get("sleep"):
    time.sleep(plan["sleep"])
for rel, content in (plan.get("write") or {}).items():
    p = pathlib.Path(rel)
    if p.parent != pathlib.Path("."):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
res = os.environ.get("DISPATCH_RESULT")
if res and plan.get("result", True):
    with open(res, "w") as f:
        json.dump({"summary": plan.get("summary", "did " + stage),
                   "artifacts": plan.get("artifacts", []),
                   "proposals": plan.get("proposals", [])}, f)
print(json.dumps({"result": plan.get("summary", "did " + stage),
                  "total_cost_usd": plan.get("usd", 0.01)}))
sys.exit(plan.get("exit", 0))
'''

#: The fixture repo's own test. It is stdlib `unittest`, not pytest, because
#: the gate really runs it: a pytest-shaped test needs pytest installed, and
#: whether the developer happens to have it then decides whether this project's
#: suite passes. It did — CI has no pytest, every card dead-lettered at its
#: first gate, and twelve tests failed for a reason nothing named.
PASSING_TEST = '''\
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from calc import add


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
'''


#: A test the gate must see fail. Same shape as PASSING_TEST for the same
#: reason: `def test_x(): assert False` is invisible to `unittest discover`,
#: so a gate that was supposed to fail quietly passes and the test asserting
#: the failure path proves nothing.
FAILING_TEST = '''\
import unittest


class TestBroken(unittest.TestCase):
    def test_broken(self):
        self.fail("deliberately broken")
'''

import contextlib
import io


@contextlib.contextmanager
def _quiet():
    """Commands print for humans; tests do not need the noise."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


class BoardCase(unittest.TestCase):
    """A fresh repo + board per test. Slower than sharing one, and worth it —
    these tests are about state machines, and shared state hides bugs."""

    #: set False for tests that only need the data model, not git
    needs_git = True

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dispatch-test-")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.root, "tests"))
        self.write("src/calc.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_calc.py", PASSING_TEST)
        self.write("pyproject.toml", '[project]\nname = "t"\n')

        if self.needs_git:
            git(self.root, "init", "-q")
            git(self.root, "config", "user.email", "t@t.t")
            git(self.root, "config", "user.name", "t")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "initial")

        with _quiet():
            # sys.executable, not `python3`: the gate runs this command, and
            # resolving it through PATH makes the result depend on which
            # interpreter happens to be first.
            rc = cli_main(["init", self.root, "--test-cmd",
                           f"{sys.executable} -m unittest discover -s tests",
                           "--no-verify"])
        assert rc == 0, "dispatch init failed"

        self.agent_path = os.path.join(self.tmp, "fake_agent.py")
        with open(self.agent_path, "w") as f:
            f.write(FAKE_AGENT)

        cfg = load_config(self.root)
        # Confinement is exercised deliberately in test_sandbox; everywhere
        # else it would make the suite depend on the host's OS support.
        cfg["sandbox"]["enabled"] = False
        cfg["runner"]["command"] = [sys.executable, self.agent_path]
        cfg["scheduler"]["tick_seconds"] = 0.05
        cfg["arbiter"]["command"] = []          # no model calls in tests
        save_config(self.root, cfg)

        self.db = DB(paths(self.root)["db"])
        self.cfg = load_config(self.root)
        self.wfs = W.load(self.db)

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def write(self, rel: str, content: str) -> str:
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def reload(self) -> None:
        self.cfg = load_config(self.root)
        self.wfs = W.load(self.db)

    def set_config(self, **path_values: Any) -> None:
        """set_config(**{"scheduler.max_concurrent": 1})"""
        cfg = load_config(self.root)
        for dotted, value in path_values.items():
            node = cfg
            parts = dotted.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = value
        save_config(self.root, cfg)
        self.reload()

    def set_workflow(self, card_type: str, stages: list[dict[str, Any]],
                     label: str | None = None) -> None:
        wfs = W.load(self.db)
        wfs[card_type] = {"label": label or card_type, "color": "#888",
                          "stages": stages}
        W.save(self.db, wfs)
        self.reload()

    def only_workflow(self, card_type: str, stages: list[dict[str, Any]]) -> None:
        W.save(self.db, {card_type: {"label": card_type, "color": "#888",
                                     "stages": stages}})
        self.reload()

    def plan_agent(self, plan: dict[str, Any]) -> None:
        """plan_agent({"build": {"write": {"src/x.py": "..."}, "exit": 1}})"""
        with open(os.path.join(self.root, ".dispatch", "fake_agent.json"), "w") as f:
            json.dump(plan, f)

    def add_card(self, title: str = "a card", **kw: Any) -> str:
        kw.setdefault("card_type", "development")
        kw.setdefault("acceptance", ["pytest passes"])
        start = kw.pop("start", True)
        tid = B.create(self.db, self.cfg, self.wfs, title=title, **kw)
        if start:
            B.start_card(self.db, self.wfs, tid)
        return tid

    def task(self, tid: str) -> dict[str, Any]:
        t = B.get(self.db, tid)
        assert t is not None, f"card {tid} vanished"
        return t

    def run_cli(self, argv: list[str]) -> tuple:
        """Returns (exit_code, stdout)."""
        with _quiet() as buf:
            rc = cli_main(argv)
        return rc, buf.getvalue()

    def scheduler(self):
        from dispatch.scheduler import Scheduler
        s = Scheduler(self.root, self.db, log=lambda m: None)
        return s

    def drain(self, sched=None, max_ticks: int = 60, until=None):
        """Tick until the board settles (or `until(self)` returns True)."""
        import time
        sched = sched or self.scheduler()
        for _ in range(max_ticks):
            sched.tick()
            if until is not None and until(self):
                return sched
            busy = any(t.is_alive() for t in sched._threads.values())
            if sched._merge_thread is not None and sched._merge_thread.is_alive():
                busy = True
            pending = self.db.q1(
                "SELECT COUNT(*) c FROM tasks WHERE status IN "
                "('queued','ready','running','leased','merging')")["c"]
            if not busy and not pending and until is None:
                return sched
            time.sleep(0.05)
        return sched

    # -- assertions ---------------------------------------------------------
    def assertStage(self, tid: str, stage: str, status: str | None = None) -> None:
        t = self.task(tid)
        self.assertEqual(t["stage"], stage,
                         f"{tid} is at {t['stage']}/{t['status']}, expected {stage}")
        if status:
            self.assertEqual(t["status"], status)

    def stages_run(self, tid: str) -> list[str]:
        return [r["stage"] for r in self.db.q(
            "SELECT stage FROM runs WHERE task_id=? ORDER BY started_at", (tid,))]

    def gate_verdicts(self, tid: str, gate: str) -> list[str]:
        return [r["verdict"] for r in self.db.q(
            "SELECT verdict FROM gate_runs WHERE task_id=? AND gate=? ORDER BY id",
            (tid, gate))]

"""Regressions from real use.

Everything here is a defect someone hit while running dispatch on live work.
"""
import json
import os
import sys
import time
from unittest import mock

from dispatch import board as B
from dispatch import gates as G
from dispatch import proposals as P
from dispatch import sandbox as SB
from dispatch.config import load_config
from tests.helpers import BoardCase, git

CALC_WITH_MUL = ("def add(a, b):\n    return a + b\n\n\n"
                 "def mul(a, b):\n    return a * b\n")


class TestStrayWrites(BoardCase):
    """An agent that resolves an absolute path writes into the main repo
    instead of its worktree. The work never reaches the card's branch, and the
    dirtied tree blocks every other card's merge — silently at both ends."""

    def setUp(self):
        super().setUp()
        self.set_config(**{"scheduler.retry_backoff_s": 0.1,
                           "runner.merge_retry_s": 0.1})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])

    def _agent_that_writes_outside(self):
        probe = os.path.join(self.tmp, "stray_agent.py")
        with open(probe, "w") as f:
            f.write(
                "import json, os, sys, pathlib\n"
                "sys.stdin.read()\n"
                "root = os.environ['DISPATCH_ROOT']\n"
                "# the bug: an absolute path back into the main repo\n"
                "pathlib.Path(root, 'src', 'leaked.py').write_text('# oops\\n')\n"
                "res = os.environ.get('DISPATCH_RESULT')\n"
                "open(res, 'w').write(json.dumps({'summary': 'all done!'}))\n"
                "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0}))\n")
        self.set_config(**{"runner.command": [sys.executable, probe]})

    def test_a_write_outside_the_worktree_is_detected(self):
        self._agent_that_writes_outside()
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertIn("src/leaked.py", res["stray_writes"])

    def test_the_agent_reporting_success_does_not_make_it_a_success(self):
        self._agent_that_writes_outside()
        tid = self.add_card(card_type="t", max_attempts=1)
        self.drain(until=lambda s: s.task(tid)["status"]
                   in (B.DEADLETTER, B.CHECKPOINT), max_ticks=200)
        self.assertNotEqual(self.task(tid)["status"], B.DONE)
        self.assertIn(G.FAIL, self.gate_verdicts(tid, "no_stray_writes"))

    def test_the_agent_is_told_exactly_what_it_did_wrong(self):
        self._agent_that_writes_outside()
        tid = self.add_card(card_type="t", max_attempts=1)
        self.drain(until=lambda s: s.task(tid)["last_evidence"], max_ticks=200)
        ev = self.task(tid)["last_evidence"] or ""
        self.assertIn("src/leaked.py", ev)
        self.assertIn("outside", ev)

    def test_a_tree_that_was_already_dirty_is_not_blamed_on_the_agent(self):
        self.write("src/preexisting.py", "# mine, not the agent's\n")
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["stray_writes"], [])

    def test_dispatchs_own_directory_is_never_counted(self):
        from dispatch.runner import dirty_paths
        self.assertFalse([p for p in dirty_paths(self.root)
                          if p.startswith(".dispatch")])

    def test_a_clean_run_reports_nothing(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["stray_writes"], [])


class TestStalledMerges(BoardCase):
    """A dirty base tree blocks every card. Waiting quietly forever is the
    worst possible response."""

    def setUp(self):
        super().setUp()
        # 0, or the loop below ticks faster than the retry backoff and only
        # ever gets one attempt in
        self.set_config(**{"runner.merge_retry_s": 0,
                           "runner.merge_busy_escalate_after": 3})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])

    def _stall(self):
        """A card ready to land, and a base tree someone has dirtied."""
        tid = self.add_card(card_type="t")
        branch = f"dispatch/{tid}"
        git(self.root, "branch", branch)
        B.update(self.db, tid, status=B.MERGING, stage="done",
                 workspace={"branch": branch, "worktree": None,
                            "base_ref": "master"})
        self.write("src/calc.py", "# an uncommitted edit\n")
        return tid

    def test_a_stalled_merge_eventually_asks_for_help(self):
        self._stall()
        sched = self.scheduler()
        for _ in range(12):
            sched.tick()
            if sched._merge_thread:
                sched._merge_thread.join(timeout=10)
            time.sleep(0.01)
        cp = self.db.q1("SELECT question, bundle FROM checkpoints "
                        "WHERE status='open'")
        self.assertIsNotNone(cp, "merges stalled silently")
        self.assertIn("Merges are stalled", cp["question"])

    def test_the_ask_names_the_files_holding_it_up(self):
        self._stall()
        sched = self.scheduler()
        for _ in range(12):
            sched.tick()
            if sched._merge_thread:
                sched._merge_thread.join(timeout=10)
            time.sleep(0.01)
        cp = self.db.q1("SELECT bundle FROM checkpoints WHERE status='open'")
        bundle = json.loads(cp["bundle"])
        self.assertIn("src/calc.py", bundle["dirty_files"])
        self.assertIn("stray_writes", bundle["note"])

    def test_it_asks_once_not_every_tick(self):
        self._stall()
        sched = self.scheduler()
        for _ in range(25):
            sched.tick()
            if sched._merge_thread:
                sched._merge_thread.join(timeout=10)
            time.sleep(0.01)
        n = self.db.q1("SELECT COUNT(*) c FROM checkpoints WHERE status='open'")["c"]
        self.assertEqual(n, 1)

    def test_blocked_explains_the_hold_where_you_would_look_for_it(self):
        tid = self._stall()
        B.update(self.db, tid, defer_reason="merge: your working tree has "
                                            "uncommitted changes to tracked files")
        blockers = B.blockers(self.db, self.cfg, self.wfs, self.task(tid))
        self.assertTrue(any("uncommitted" in b for b in blockers), blockers)


class TestSandboxDefault(BoardCase):
    needs_git = False

    def _shipped(self):
        from dispatch.config import DEFAULT_CONFIG
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def test_the_default_confines_wherever_the_os_can(self):
        # the stray-write failure only happens unconfined, and off-by-default
        # made that the out-of-the-box behaviour
        cfg = self._shipped()
        self.assertEqual(cfg["sandbox"]["enabled"], "auto")
        with mock.patch.object(SB, "backend_available", return_value=True):
            self.assertTrue(SB.enabled(cfg))

    def test_auto_degrades_to_a_warning_rather_than_refusing_to_run(self):
        cfg = self._shipped()
        with mock.patch.object(SB, "backend_available", return_value=False):
            ok, problems = SB.preflight(cfg, self.root)
            self.assertTrue(ok, "auto must not brick a host with no backend")
            self.assertFalse(SB.enabled(cfg))
        self.assertTrue(any("NOT confined" in p for p in problems), problems)

    def test_true_still_means_refuse_to_run_without_it(self):
        cfg = {"sandbox": {"enabled": True}}
        self.assertTrue(SB.required(cfg))
        with mock.patch.object(SB, "backend_available", return_value=False):
            ok, _ = SB.preflight(cfg, self.root)
        self.assertFalse(ok)

    def test_false_is_off_and_says_what_that_costs(self):
        cfg = {"sandbox": {"enabled": False}}
        self.assertFalse(SB.enabled(cfg))
        ok, problems = SB.preflight(cfg, self.root)
        self.assertTrue(ok)
        self.assertTrue(any("absolute path" in p for p in problems), problems)


class TestEdgeRemoval(BoardCase):
    needs_git = False

    def test_an_edge_can_be_removed_again(self):
        a, b = self.add_card("a"), self.add_card("b")
        B.link(self.db, a, b)
        rc, _ = self.run_cli(["--root", self.root, "unlink", a, b])
        self.assertEqual(rc, 0)
        self.assertEqual(B.deps_of(self.db, b), [])

    def test_removing_an_edge_that_is_not_there_says_so(self):
        a, b = self.add_card("a"), self.add_card("b")
        rc, out = self.run_cli(["--root", self.root, "unlink", a, b])
        self.assertEqual(rc, 1)
        self.assertIn("no finish_to_start edge", out)

    def test_edges_can_be_listed(self):
        a, b = self.add_card("a"), self.add_card("b")
        B.link(self.db, a, b, "artifact")
        _rc, out = self.run_cli(["--root", self.root, "edges"])
        self.assertIn("artifact", out)
        self.assertIn(a, out)


class TestAncestorDeadlock(BoardCase):
    needs_git = False

    def test_a_card_cannot_depend_on_its_own_parent(self):
        # the parent waits for its children at its final stage, so this
        # deadlocks both — and it is provable when the edge is created
        parent = self.add_card("epic")
        child = self.add_card("piece", parent_id=parent)
        with self.assertRaises(ValueError) as ctx:
            B.link(self.db, parent, child)
        self.assertIn("deadlock", str(ctx.exception))

    def test_it_catches_a_grandparent_too(self):
        a = self.add_card("a")
        b = self.add_card("b", parent_id=a)
        c = self.add_card("c", parent_id=b)
        with self.assertRaises(ValueError):
            B.link(self.db, a, c)

    def test_a_sibling_dependency_is_still_fine(self):
        parent = self.add_card("epic")
        x = self.add_card("x", parent_id=parent)
        y = self.add_card("y", parent_id=parent)
        self.assertIsNotNone(B.link(self.db, x, y))

    def test_the_cli_reports_it_instead_of_stalling_at_runtime(self):
        parent = self.add_card("epic")
        child = self.add_card("piece", parent_id=parent)
        rc, out = self.run_cli(["--root", self.root, "link", parent, child])
        self.assertEqual(rc, 1)
        self.assertIn("deadlock", out)


class TestCancelSafety(BoardCase):
    needs_git = False

    def test_it_refuses_to_take_children_silently(self):
        parent = self.add_card("parent")
        child = self.add_card("a running child", parent_id=parent)
        B.update(self.db, child, status=B.RUNNING)
        rc, out = self.run_cli(["--root", self.root, "cancel", parent])
        self.assertEqual(rc, 1)
        self.assertIn("a running child", out, "it must name what it would destroy")
        self.assertEqual(self.task(child)["status"], B.RUNNING)
        self.assertNotEqual(self.task(parent)["status"], B.CANCELLED)

    def test_cascade_is_the_opt_in(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        rc, out = self.run_cli(["--root", self.root, "cancel", parent, "--cascade"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task(child)["status"], B.CANCELLED)
        self.assertIn(child, out)

    def test_only_leaves_the_children_alone(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        rc, _ = self.run_cli(["--root", self.root, "cancel", parent, "--only"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task(parent)["status"], B.CANCELLED)
        self.assertNotEqual(self.task(child)["status"], B.CANCELLED)

    def test_a_childless_card_needs_no_ceremony(self):
        a = self.add_card("alone")
        rc, _ = self.run_cli(["--root", self.root, "cancel", a])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task(a)["status"], B.CANCELLED)


class TestMachineReadableLs(BoardCase):
    needs_git = False

    def test_ls_has_a_json_form(self):
        self.add_card("a card", tags=["api"])
        _rc, out = self.run_cli(["--root", self.root, "ls", "--json"])
        rows = json.loads(out[out.index("["):])
        self.assertEqual(rows[0]["title"], "a card")
        self.assertIn("blocked_by", rows[0])

    def test_the_id_is_always_the_first_field(self):
        # a marker in front of it shifted every column and broke parsing
        a = self.add_card("a")
        B.update(self.db, a, status=B.RUNNING)
        self.db.x("INSERT INTO leases (task_id,run_id,heartbeat_at,expires_at) "
                  "VALUES (?,?,?,?)", (a, "r_1", 0, 9e9))
        _rc, out = self.run_cli(["--root", self.root, "ls"])
        row = next(ln for ln in out.splitlines() if a in ln)
        self.assertTrue(row.split()[0] == a, f"first field was {row.split()[0]!r}")


class TestShowIsHonest(BoardCase):
    needs_git = False

    def test_a_clipped_brief_says_that_it_was_clipped(self):
        long = "x" * 9000
        a = self.add_card("a", brief=long)
        _rc, out = self.run_cli(["--root", self.root, "show", a])
        self.assertIn("clipped", out)

    def test_full_shows_all_of_it(self):
        a = self.add_card("a", brief="y" * 9000)
        _rc, out = self.run_cli(["--root", self.root, "show", a, "--full"])
        self.assertNotIn("clipped", out)
        self.assertIn("y" * 8000, out)

    def test_editing_a_running_card_explains_when_the_edit_lands(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.RUNNING)
        _rc, out = self.run_cli(["--root", self.root, "show", a])
        self.assertIn("next attempt", out)


class TestDuplicateBands(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])

    def test_reworded_titles_score_higher_than_character_similarity_alone(self):
        import difflib
        a = "Add a Retry-After header to 429 responses"
        b = "429 responses should include the Retry-After header"
        seq = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
        self.assertGreater(P.similarity(a, b), seq)

    def test_unrelated_work_is_not_flagged(self):
        self.assertLess(P.similarity("Rewrite the billing exporter",
                                     "Add rate limiting to the API"), 0.4)

    def test_an_uncertain_duplicate_is_judged_rather_than_accepted_blind(self):
        self.add_card("Add a Retry-After header to 429 responses", card_type="t")
        pid = P.submit(self.db, from_task=None, kind="add_task",
                       payload={"title": "429 responses should include the "
                                         "Retry-After header"})
        prop = dict(self.db.q1("SELECT * FROM proposals WHERE id=?", (pid,)))
        near, score = P._suspected_duplicate(self.db, self.cfg, prop)
        self.assertIsNotNone(near, f"scored {score:.2f}, below the review band")

    def test_an_obvious_duplicate_still_merges_without_a_model(self):
        self.add_card("Add rate limiting to the public API", card_type="t")
        before = self.db.q1("SELECT COUNT(*) c FROM tasks")["c"]
        pid = P.submit(self.db, from_task=None, kind="add_task",
                       payload={"title": "Add rate limiting to the public API"})
        prop = dict(self.db.q1("SELECT * FROM proposals WHERE id=?", (pid,)))
        P.adjudicate(self.db, self.root, self.cfg, self.wfs, prop)
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM tasks")["c"], before)


class TestInitVerifiesTheTestCommand(BoardCase):
    needs_git = False

    def _fresh(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="dispatch-verify-")
        git(d, "init", "-q")
        return d

    def test_a_detected_command_that_does_not_work_is_not_stored(self):
        # it wrote `pytest -q` for a Godot project, and a wrong command makes
        # every completion gate meaningless while the board looks healthy
        d = self._fresh()
        os.makedirs(os.path.join(d, "tests"))   # trips the pytest heuristic
        with open(os.path.join(d, "project.godot"), "w") as f:
            f.write("[application]\n")
        rc, out = self.run_cli(["init", d])
        self.assertEqual(rc, 0)
        self.assertEqual(load_config(d)["commands"]["test"], "")
        self.assertIn("NOT stored", out)

    def test_an_explicit_command_is_stored_but_the_failure_is_reported(self):
        d = self._fresh()
        _rc, out = self.run_cli(["init", d, "--test-cmd", "false"])
        self.assertEqual(load_config(d)["commands"]["test"], "false")
        self.assertIn("warning", out)

    def test_a_working_command_is_verified(self):
        d = self._fresh()
        _rc, out = self.run_cli(["init", d, "--test-cmd", "true"])
        self.assertEqual(load_config(d)["commands"]["test"], "true")
        self.assertIn("verified", out)

    def test_the_test_command_can_be_changed_without_force(self):
        # having to hand-edit config.json turned a 30-second fix into ten minutes
        d = self._fresh()
        self.run_cli(["init", d, "--test-cmd", "true"])
        rc, out = self.run_cli(["init", d, "--test-cmd", "true"])
        self.assertEqual(rc, 0)
        self.assertIn("updated", out)

    def test_a_bare_re_init_still_refuses_and_says_how(self):
        d = self._fresh()
        self.run_cli(["init", d, "--no-verify"])
        rc, out = self.run_cli(["init", d])
        self.assertEqual(rc, 1)
        self.assertIn("--test-cmd", out)

    def test_a_missing_binary_is_caught_without_running_anything(self):
        from dispatch.cli import _verify_command
        ok, detail = _verify_command(self.root, "definitely-not-a-real-binary x")
        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

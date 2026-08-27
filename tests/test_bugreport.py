"""Regressions from a four-hour session on a real Godot project."""
import json

from dispatch import board as B
from dispatch import gates as G
from dispatch import merge as M
from dispatch import proposals as P
from tests.helpers import BoardCase, git


class TestDoneMeansLanded(BoardCase):
    """The quietest failure this system can produce: the board says `done`, and
    the work exists only on a branch nobody looks at again."""

    def setUp(self):
        super().setUp()
        self.set_config(**{"runner.merge_retry_s": 0,
                           "runner.verify_landed_every_ticks": 1})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])

    def _card_with_unlanded_work(self):
        tid = self.add_card(card_type="t")
        branch = f"dispatch/{tid}"
        git(self.root, "checkout", "-q", "-b", branch)
        self.write("src/extra.py", "# work that never landed\n")
        git(self.root, "add", "src/extra.py")
        git(self.root, "commit", "-qm", "work on the branch")
        git(self.root, "checkout", "-q", "master")
        B.update(self.db, tid, stage="done", status=B.DONE,
                 workspace={"branch": branch, "worktree": None,
                            "base_ref": "master"})
        return tid

    def test_unlanded_commits_are_counted(self):
        tid = self._card_with_unlanded_work()
        self.assertEqual(M.unlanded(self.root, self.cfg, self.task(tid)), 1)

    def test_a_landed_card_counts_zero(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, workspace={"branch": "master",
                                          "base_ref": "master"})
        self.assertEqual(M.unlanded(self.root, self.cfg, self.task(tid)), 0)

    def test_a_card_called_done_with_work_left_is_sent_back(self):
        tid = self._card_with_unlanded_work()
        sched = self.scheduler()
        sched.ticks = 0
        sched.verify_landed()
        self.assertEqual(self.task(tid)["status"], B.MERGING,
                         "a done card with unlanded work stayed done")
        self.assertTrue(self.db.q1(
            "SELECT id FROM events WHERE kind='merge.unlanded'"))

    def test_a_card_queued_at_stage_done_is_not_a_resting_state(self):
        # REGRESSION: approving a merge-conflict checkpoint left the card
        # queued at stage `done`, which nothing picks up — it looked finished
        # and never retried
        tid = self._card_with_unlanded_work()
        B.update(self.db, tid, status=B.QUEUED)
        sched = self.scheduler()
        sched.ticks = 0
        sched.verify_landed()
        self.assertEqual(self.task(tid)["status"], B.MERGING)

    def test_a_properly_landed_card_is_left_alone(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, stage="done", status=B.DONE,
                 workspace={"branch": "master", "base_ref": "master"})
        sched = self.scheduler()
        sched.ticks = 0
        sched.verify_landed()
        self.assertEqual(self.task(tid)["status"], B.DONE)

    def test_approving_a_merge_failure_retries_the_merge(self):
        tid = self._card_with_unlanded_work()
        cid = B.open_checkpoint(self.db, tid, kind="escalation",
                                question="will not land")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        self.assertEqual(self.task(tid)["status"], B.MERGING,
                         "approving left it in a state nothing picks up")

    def test_approving_an_ordinary_escalation_still_just_requeues(self):
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, kind="escalation",
                                question="no acceptance criteria")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        self.assertEqual(self.task(tid)["status"], B.QUEUED)


class TestExpansionAlarmAttribution(BoardCase):
    needs_git = False

    def test_cards_you_create_are_not_the_board_expanding_itself(self):
        self.set_config(**{"containment.expansion_ratio_window": 4})
        for i in range(12):
            self.db.emit("task.created", f"t_{i}", actor="human")
        self.db.emit("task.done", "t_0")
        ratio, created, _ = P.expansion_ratio(self.db, self.cfg)
        self.assertEqual(created, 0, "operator churn counted as agent expansion")
        self.assertEqual(ratio, 0.0)

    def test_agent_created_cards_do_count(self):
        self.set_config(**{"containment.expansion_ratio_window": 4})
        for i in range(12):
            self.db.emit("task.created", f"t_{i}", actor="agent:t_x")
        self.db.emit("task.done", "t_0")
        ratio, created, done = P.expansion_ratio(self.db, self.cfg)
        self.assertGreater(created, done * 2, f"{created} created / {done} done")
        self.assertGreater(ratio, 2.5)

    def test_acknowledging_it_restarts_the_window(self):
        # REGRESSION: the ratio was purely historical, so an answered alarm
        # re-fired forever and the only way through was disabling the guard
        self.set_config(**{"containment.expansion_ratio_window": 4})
        for i in range(12):
            self.db.emit("task.created", f"t_{i}", actor="agent:t_x")
        self.assertGreater(P.expansion_ratio(self.db, self.cfg)[0], 2.5)
        P.acknowledge_expansion(self.db)
        self.assertEqual(P.expansion_ratio(self.db, self.cfg)[0], 0.0)

    def test_answering_the_alarms_checkpoint_clears_the_pause(self):
        from dispatch.config import load_config
        self.set_config(**{"containment.expansion_ratio_window": 4,
                           "containment.expansion_ratio_limit": 2.0})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.add_card(card_type="t")
        for i in range(12):
            self.db.emit("task.created", f"t_{i}", actor="agent:t_x")
        sched = self.scheduler()
        sched.check_expansion()
        self.assertTrue(load_config(self.root)["scheduler"]["paused"])

        cp = self.db.q1("SELECT id FROM checkpoints WHERE status='open'")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cp["id"], "approve")
        cfg = load_config(self.root)
        self.assertFalse(cfg["scheduler"]["paused"], "the pause outlived its answer")
        self.assertEqual(P.expansion_ratio(self.db, cfg)[0], 0.0)

    def test_the_pause_records_why(self):
        from dispatch.config import load_config
        self.set_config(**{"containment.expansion_ratio_window": 4,
                           "containment.expansion_ratio_limit": 2.0})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.add_card(card_type="t")
        for i in range(12):
            self.db.emit("task.created", f"t_{i}", actor="agent:t_x")
        self.scheduler().check_expansion()
        why = load_config(self.root)["scheduler"]["paused_reason"]
        self.assertIn("expansion alarm", why)

    def test_status_tells_a_pause_apart_from_a_crash(self):
        self.set_config(**{"scheduler.paused": True,
                           "scheduler.paused_reason": "expansion alarm — 14 per 5"})
        _rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertIn("paused", out)
        self.assertIn("expansion alarm", out)

    def test_resume_clears_it(self):
        from dispatch.config import load_config
        self.set_config(**{"scheduler.paused": True,
                           "scheduler.paused_reason": "expansion alarm"})
        rc, _out = self.run_cli(["--root", self.root, "resume"])
        self.assertEqual(rc, 0)
        self.assertFalse(load_config(self.root)["scheduler"]["paused"])


class TestScopeAttribution(BoardCase):
    needs_git = False

    def _ctx(self, task, **extra):
        from dispatch.config import paths
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
               "root": self.root, "paths": paths(self.root), "task": task}
        ctx.update(extra)
        return ctx

    def test_litter_that_predated_the_card_is_not_blamed_on_it(self):
        # engine-generated sidecars had been there for a week; the gate blamed
        # an agent that was blameless
        tid = self.add_card(scope=["src/**"])
        ws = dict(self.task(tid)["workspace"])
        ws["pre_existing"] = ["engine/.godot/uid_cache.bin", "engine/x.import"]
        B.update(self.db, tid, workspace=ws)
        ctx = self._ctx(self.task(tid),
                        changed_files=["src/ok.py", "engine/.godot/uid_cache.bin",
                                       "engine/x.import"])
        self.assertEqual(G.BUILTINS["diff_scope"](ctx, []).verdict, G.PASS)

    def test_a_genuine_stray_is_still_caught(self):
        tid = self.add_card(scope=["src/**"])
        ws = dict(self.task(tid)["workspace"])
        ws["pre_existing"] = ["engine/x.import"]
        B.update(self.db, tid, workspace=ws)
        ctx = self._ctx(self.task(tid),
                        changed_files=["src/ok.py", "engine/x.import",
                                       "infra/deploy.tf"])
        v = G.BUILTINS["diff_scope"](ctx, [])
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("infra/deploy.tf", v.evidence)
        self.assertNotIn("engine/x.import", v.evidence)


class TestProposalsCarryCriteria(BoardCase):
    needs_git = False

    def test_add_task_without_criteria_is_refused_at_the_source(self):
        # 5 for 5 arrived with none; the gate caught them all, but each one
        # spent a human's attention
        tid = self.add_card()
        rc, out = self.run_cli(["--root", self.root, "propose", "--from", tid,
                                "--kind", "add_task", "--title", "more work"])
        self.assertEqual(rc, 1)
        self.assertIn("--accept is required", out)
        self.assertIsNone(self.db.q1("SELECT id FROM proposals"))

    def test_criteria_reach_the_card_that_gets_created(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        tid = self.add_card(card_type="t")
        self.run_cli(["--root", self.root, "propose", "--from", tid,
                      "--kind", "add_task", "--title", "follow-up work",
                      "--accept", "pytest tests/test_follow.py passes",
                      "--scope", "src/**"])
        prop = dict(self.db.q1("SELECT * FROM proposals"))
        P.adjudicate(self.db, self.root, self.cfg, self.wfs, prop)
        made = self.db.q1("SELECT * FROM tasks WHERE title='follow-up work'")
        self.assertIsNotNone(made)
        self.assertIn("pytest tests/test_follow.py passes",
                      json.loads(made["acceptance"]))

    def test_other_kinds_do_not_need_criteria(self):
        tid = self.add_card()
        rc, _ = self.run_cli(["--root", self.root, "propose", "--from", tid,
                              "--kind", "raise_blocker", "--reason", "stuck"])
        self.assertEqual(rc, 0)


class TestBoardWideBudget(BoardCase):
    needs_git = False

    def _ctx(self, task):
        from dispatch.config import paths
        return {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
                "root": self.root, "paths": paths(self.root), "task": task}

    def _spend(self, usd):
        from dispatch.db import new_id
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,usd,"
                  "started_at) VALUES (?,?,?,?,?,?,?)",
                  (new_id("r"), self.add_card("x"), "build", "developer",
                   "finished", usd, 0))

    def test_the_subtree_ceiling_is_not_a_board_ceiling(self):
        # ten cards each under $25 spent $39.58 with no signal
        self.set_config(**{"containment.total_budget_usd": 25.0})
        self._spend(20.0)
        self._spend(20.0)
        tid = self.add_card("a card")
        v = G.BUILTINS["budget_remaining"](self._ctx(self.task(tid)), [])
        self.assertEqual(v.verdict, G.ESCALATE)
        self.assertIn("40.00", v.reason)

    def test_no_ceiling_configured_means_no_ceiling(self):
        self._spend(500.0)
        tid = self.add_card("a card")
        self.assertEqual(
            G.BUILTINS["budget_remaining"](self._ctx(self.task(tid)), []).verdict,
            G.PASS)

    def test_status_shows_the_total_against_the_ceiling(self):
        self.set_config(**{"containment.total_budget_usd": 25.0})
        self._spend(30.0)
        _rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertIn("CEILING REACHED", out)

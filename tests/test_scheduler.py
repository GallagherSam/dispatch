"""The loop.

These are the tests that matter most: the scheduler is the thing that must never
decide the work is finished.
"""
import os
import time

from dispatch import board as B
from dispatch import gates as G
from tests.helpers import BoardCase

CALC_WITH_MUL = ("def add(a, b):\n    return a + b\n\n\n"
                 "def mul(a, b):\n    return a * b\n")


class TestHappyPath(BoardCase):
    def test_a_card_walks_its_whole_pipeline_unattended(self):
        self.only_workflow("t", [
            {"stage": "build", "agent": "developer", "gates": ["tests_pass"]},
            {"stage": "qa", "agent": "qa", "gates": ["tests_pass"]},
            {"stage": "review", "agent": "reviewer"},
        ])
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain()
        self.assertStage(tid, "done", B.DONE)
        self.assertEqual(self.stages_run(tid), ["build", "qa", "review"])

    def test_each_stage_runs_under_its_own_agent(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "qa", "agent": "qa"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        self.drain()
        agents = [r["agent_type"] for r in self.db.q(
            "SELECT agent_type FROM runs WHERE task_id=? ORDER BY started_at",
            (tid,))]
        self.assertEqual(agents, ["developer", "qa"])

    def test_dependencies_are_respected(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        first = self.add_card("first", card_type="t")
        second = self.add_card("second", card_type="t")
        B.link(self.db, first, second)
        self.drain()
        order = [r["task_id"] for r in self.db.q(
            "SELECT task_id FROM runs ORDER BY started_at")]
        self.assertLess(order.index(first), order.index(second))


class TestFailureHandling(BoardCase):
    def setUp(self):
        super().setUp()
        self.set_config(**{"scheduler.retry_backoff_s": 0.1})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["tests_pass"]}])

    def test_a_failing_gate_returns_the_card_with_evidence(self):
        self.plan_agent({"*": {"write": {
            "tests/test_calc.py": "def test_broken():\n    assert False\n"}}})
        tid = self.add_card(card_type="t", max_attempts=2)
        sched = self.scheduler()
        for _ in range(20):
            sched.tick()
            if self.task(tid)["attempts"] >= 1:
                break
            time.sleep(0.05)
        t = self.task(tid)
        self.assertGreaterEqual(t["attempts"], 1)
        self.assertIn("tests_pass", t["last_evidence"])

    def test_attempts_exhausted_lands_in_the_dead_letter_column(self):
        self.plan_agent({"*": {"write": {
            "tests/test_calc.py": "def test_broken():\n    assert False\n"}}})
        tid = self.add_card(card_type="t", max_attempts=2)
        self.drain(until=lambda s: s.task(tid)["status"] == B.DEADLETTER,
                   max_ticks=200)
        # quarantined, not endlessly retried -- one poison card must not eat a
        # night's quota
        self.assertEqual(self.task(tid)["status"], B.DEADLETTER)
        self.assertEqual(self.task(tid)["attempts"], 2)
        self.assertTrue(self.db.q1(
            "SELECT id FROM checkpoints WHERE task_id=? AND status='open'",
            (tid,)), "a dead-lettered card must still ask for you")

    def test_an_agent_that_dies_without_a_diff_is_a_failure(self):
        self.plan_agent({"*": {"exit": 1, "result": False}})
        tid = self.add_card(card_type="t", max_attempts=1)
        self.drain(until=lambda s: s.task(tid)["status"] == B.DEADLETTER,
                   max_ticks=200)
        self.assertEqual(self.task(tid)["status"], B.DEADLETTER)

    def test_a_stray_file_is_caught_by_diff_scope(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL,
                                         "infra/deploy.tf": "resource {}\n"}}})
        tid = self.add_card(card_type="t", scope=["src/**"], max_attempts=1)
        self.drain(until=lambda s: s.task(tid)["status"] == B.DEADLETTER,
                   max_ticks=200)
        verdicts = self.gate_verdicts(tid, "diff_scope")
        self.assertIn(G.FAIL, verdicts)
        self.assertIn("infra/deploy.tf", self.task(tid)["last_evidence"])


class TestDeferDoesNotPunish(BoardCase):
    def test_a_deferred_card_keeps_its_attempts(self):
        # the whole point of separating defer from fail: a quota gate can hold a
        # card for six hours without poisoning it
        import stat
        gate = os.path.join(self.root, ".dispatch", "gates", "not_yet.sh")
        with open(gate, "w") as f:
            f.write("#!/usr/bin/env bash\ncat > /dev/null\n"
                    'echo \'{"verdict":"defer","reason":"waiting","retry_after_s":600}\'\n')
        os.chmod(gate, os.stat(gate).st_mode | stat.S_IEXEC)
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": [{"gate": "not_yet",
                                             "hook": "pre_dispatch"}]}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        sched = self.scheduler()
        for _ in range(5):
            sched.tick()
        t = self.task(tid)
        self.assertEqual(t["attempts"], 0)
        self.assertGreater(t["defer_until"], time.time())
        self.assertIn("waiting", t["defer_reason"])
        self.assertEqual(self.stages_run(tid), [])


class TestConcurrencyControls(BoardCase):
    def test_max_concurrent_is_honoured(self):
        self.set_config(**{"scheduler.max_concurrent": 1})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {"sleep": 1.5}})
        for i in range(3):
            self.add_card(f"card {i}", card_type="t")
        sched = self.scheduler()
        sched.tick()
        time.sleep(0.4)
        sched.tick()
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM leases")["c"], 1)
        for t in sched._threads.values():
            t.join(timeout=5)

    def test_a_mutex_edge_keeps_two_cards_apart(self):
        self.set_config(**{"scheduler.max_concurrent": 4})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {"sleep": 1.5}})
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t")
        B.link(self.db, a, b, "mutex")
        sched = self.scheduler()
        sched.tick()
        time.sleep(0.3)
        sched.tick()
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM leases")["c"], 1)
        blocked = self.task(b) if self.task(a)["status"] == B.RUNNING else self.task(a)
        self.assertIn("mutex", (blocked["defer_reason"] or ""))
        for t in sched._threads.values():
            t.join(timeout=5)

    def test_a_stage_lock_serialises_integration(self):
        self.set_config(**{"scheduler.max_concurrent": 4})
        self.only_workflow("t", [{"stage": "integrate", "agent": "integrator",
                                  "lock": "integration"}])
        self.plan_agent({"*": {"sleep": 1.5}})
        for i in range(3):
            self.add_card(f"card {i}", card_type="t")
        sched = self.scheduler()
        sched.tick()
        time.sleep(0.3)
        sched.tick()
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM leases")["c"], 1)
        for t in sched._threads.values():
            t.join(timeout=5)


class TestHumanStages(BoardCase):
    def test_a_human_stage_opens_a_signoff_checkpoint(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "signoff", "agent": "human"}])
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self.assertStage(tid, "signoff", B.CHECKPOINT)
        cp = self.db.q1("SELECT kind FROM checkpoints WHERE task_id=? "
                        "AND status='open'", (tid,))
        self.assertEqual(cp["kind"], "signoff")

    def test_the_checkpoint_carries_the_diff_and_the_summary(self):
        import json
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "signoff", "agent": "human"}])
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL},
                               "summary": "I added mul()"}})
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        bundle = json.loads(self.db.q1(
            "SELECT bundle FROM checkpoints WHERE task_id=?", (tid,))["bundle"])
        self.assertIn("def mul", bundle["diff"])
        self.assertEqual(bundle["summary"], "I added mul()")
        self.assertEqual(bundle["changed_files"], ["src/calc.py"])

    def test_a_small_green_change_passes_without_waking_you(self):
        self.only_workflow("t", [
            {"stage": "build", "agent": "developer"},
            {"stage": "signoff", "agent": "human",
             "auto_pass_if": "small_and_green"},
        ])
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain()
        self.assertStage(tid, "done", B.DONE)
        self.assertTrue(self.db.q1(
            "SELECT id FROM events WHERE kind='checkpoint.auto_passed'"))

    def test_a_large_change_still_asks(self):
        big = "def add(a, b):\n    return a + b\n" + \
              "".join(f"\n\ndef f{i}(x):\n    return x + {i}\n" for i in range(15))
        self.only_workflow("t", [
            {"stage": "build", "agent": "developer"},
            {"stage": "signoff", "agent": "human",
             "auto_pass_if": "small_and_green"},
        ])
        self.plan_agent({"*": {"write": {"src/calc.py": big}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self.assertEqual(self.task(tid)["status"], B.CHECKPOINT)

    def test_a_checkpoint_holds_only_its_own_subtree(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "signoff", "agent": "human"}])
        self.plan_agent({"*": {}})
        held = self.add_card("needs a human", card_type="t")
        downstream = self.add_card("after the human", card_type="t")
        B.link(self.db, held, downstream)
        elsewhere = self.add_card("unrelated branch", card_type="t")
        self.drain(until=lambda s: s.task(held)["status"] == B.CHECKPOINT
                   and s.task(elsewhere)["status"] == B.CHECKPOINT,
                   max_ticks=80)
        self.assertEqual(self.task(held)["status"], B.CHECKPOINT)
        self.assertEqual(self.task(elsewhere)["status"], B.CHECKPOINT)
        self.assertEqual(self.stages_run(downstream), [])


class TestLeases(BoardCase):
    def test_an_expired_lease_is_reaped_and_the_card_requeued(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, status=B.RUNNING)
        self.db.x("INSERT INTO leases (task_id,run_id,pid,stage,heartbeat_at,"
                  "expires_at) VALUES (?,?,?,?,?,?)",
                  (tid, "r_ghost", 999999, "build", 0, 0))
        sched = self.scheduler()
        sched.reap_leases()
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM leases")["c"], 0)
        self.assertEqual(self.task(tid)["status"], B.QUEUED)
        self.assertTrue(self.db.q1(
            "SELECT id FROM events WHERE kind='lease.expired'"))


class TestExpansionAlarm(BoardCase):
    def test_a_runaway_board_pauses_dispatch_and_asks_for_you(self):
        from dispatch.config import load_config
        self.set_config(**{"containment.expansion_ratio_window": 4,
                           "containment.expansion_ratio_limit": 2.0})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.add_card("a real card", card_type="t")
        for i in range(10):
            self.db.emit("task.created", f"t_ghost{i}")
        sched = self.scheduler()
        sched.check_expansion()
        self.assertTrue(load_config(self.root)["scheduler"]["paused"])
        cp = self.db.q1("SELECT question FROM checkpoints WHERE status='open'")
        self.assertIn("Expansion alarm", cp["question"])

    def test_a_paused_scheduler_dispatches_nothing(self):
        self.set_config(**{"scheduler.paused": True})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        sched = self.scheduler()
        for _ in range(3):
            sched.tick()
        self.assertEqual(self.stages_run(tid), [])


class TestProposalsInTheLoop(BoardCase):
    def test_an_agent_proposal_becomes_a_card_the_scheduler_then_runs(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"build": {"proposals": [
            {"kind": "add_task",
             "payload": {"title": "the follow-up work", "acceptance": ["ok"]},
             "rationale": "discovered while working"}]}})
        tid = self.add_card(card_type="t")
        self.drain(max_ticks=80)
        made = self.db.q1("SELECT id, provenance FROM tasks WHERE title=?",
                          ("the follow-up work",))
        self.assertIsNotNone(made)
        self.assertEqual(made["provenance"], f"agent:{tid}")
        self.assertIn("build", self.stages_run(made["id"]))


class TestCheckpointSLA(BoardCase):
    """An unanswered checkpoint should park cleanly, not hang forever."""

    def _age_checkpoint(self, tid, seconds):
        cp = self.db.q1("SELECT id FROM checkpoints WHERE task_id=? "
                        "AND status='open'", (tid,))
        self.db.x("UPDATE checkpoints SET created_at=? WHERE id=?",
                  (time.time() - seconds, cp["id"]))
        return cp["id"]

    def _pipeline(self, **stage_kw):
        self.only_workflow("t", [
            {"stage": "build", "agent": "developer"},
            dict({"stage": "signoff", "agent": "human"}, **stage_kw),
            {"stage": "integrate", "agent": "integrator"},
        ])
        self.plan_agent({"*": {}})

    def test_sla_accepts_readable_durations(self):
        from dispatch.scheduler import _sla_seconds
        self.assertEqual(_sla_seconds("4h"), 14400)
        self.assertEqual(_sla_seconds("30m"), 1800)
        self.assertEqual(_sla_seconds("2d"), 172800)
        self.assertEqual(_sla_seconds(90), 90)
        self.assertIsNone(_sla_seconds(None))
        self.assertIsNone(_sla_seconds("nonsense"))

    def test_no_sla_means_it_waits_forever(self):
        self._pipeline()
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self._age_checkpoint(tid, 10 ** 6)
        sched = self.scheduler()
        sched.expire_checkpoints()
        self.assertEqual(self.task(tid)["status"], B.CHECKPOINT)

    def test_an_unanswered_checkpoint_parks_the_card(self):
        self._pipeline(sla="1h")
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        cid = self._age_checkpoint(tid, 7200)
        self.scheduler().expire_checkpoints()
        t = self.task(tid)
        self.assertEqual(t["status"], B.BLOCKED)
        self.assertIn("no answer", t["block_reason"])
        self.assertEqual(self.db.q1("SELECT status FROM checkpoints WHERE id=?",
                                    (cid,))["status"], "expired")

    def test_on_sla_approve_lets_the_card_carry_on(self):
        self._pipeline(sla="1h", on_sla="approve")
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self._age_checkpoint(tid, 7200)
        self.scheduler().expire_checkpoints()
        self.assertStage(tid, "integrate")

    def test_on_sla_reject_sends_it_back(self):
        self._pipeline(sla="1h", on_sla="reject")
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self._age_checkpoint(tid, 7200)
        self.scheduler().expire_checkpoints()
        self.assertStage(tid, "build")

    def test_a_fresh_checkpoint_is_left_alone(self):
        self._pipeline(sla="1h")
        tid = self.add_card(card_type="t")
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT)
        self.scheduler().expire_checkpoints()
        self.assertEqual(self.task(tid)["status"], B.CHECKPOINT)

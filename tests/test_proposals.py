"""Workers propose, the board disposes.

These are the rules that stop a fleet of agents talking itself into a
four-hundred-card backlog, so they get the most coverage.
"""
import json

from dispatch import board as B
from dispatch import proposals as P
from tests.helpers import BoardCase


class ProposalCase(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])

    def propose(self, kind, payload, from_task=None, **kw):
        pid = P.submit(self.db, from_task=from_task, kind=kind, payload=payload, **kw)
        return dict(self.db.q1("SELECT * FROM proposals WHERE id=?", (pid,)))

    def adjudicate(self, prop):
        return P.adjudicate(self.db, self.root, self.cfg, self.wfs, prop)


class TestInvariants(ProposalCase):
    def test_an_agent_may_not_complete_its_own_card(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("amend_brief", {"task_id": tid, "status": "done"},
                            from_task=tid)
        self.assertEqual(self.adjudicate(prop), "rejected")
        self.assertNotEqual(self.task(tid)["status"], B.DONE)

    def test_an_agent_may_not_amend_its_own_gates(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("amend_brief", {"task_id": tid, "gates": []},
                            from_task=tid)
        self.assertEqual(self.adjudicate(prop), "rejected")

    def test_an_agent_may_not_amend_its_own_acceptance_criteria(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("amend_brief",
                            {"task_id": tid, "acceptance": ["anything goes"]},
                            from_task=tid)
        self.assertEqual(self.adjudicate(prop), "rejected")
        self.assertEqual(self.task(tid)["acceptance"], ["pytest passes"])

    def test_an_agent_may_amend_another_card(self):
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t", brief="original")
        prop = self.propose("amend_brief", {"task_id": b, "append": "extra note"},
                            from_task=a)
        self.assertEqual(self.adjudicate(prop), "accepted")
        self.assertIn("extra note", self.task(b)["brief"])

    def test_a_dependency_that_would_cycle_is_rejected(self):
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t")
        B.link(self.db, a, b)
        prop = self.propose("add_dep", {"src": b, "dst": a}, from_task=a)
        self.assertEqual(self.adjudicate(prop), "rejected")

    def test_an_agent_may_not_cancel_its_own_card(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("cancel", {"task_id": tid}, from_task=tid)
        self.adjudicate(prop)
        self.assertNotEqual(self.task(tid)["status"], B.CANCELLED)

    def test_depth_is_capped(self):
        self.set_config(**{"containment.max_depth": 1})
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t", parent_id=a)
        prop = self.propose("split", {"tasks": [{"title": "too deep"}]},
                            from_task=b)
        self.assertEqual(self.adjudicate(prop), "rejected")

    def test_fan_out_is_capped(self):
        self.set_config(**{"containment.max_children_per_parent": 2})
        parent = self.add_card("parent", card_type="t")
        for i in range(2):
            self.add_card(f"kid{i}", card_type="t", parent_id=parent)
        prop = self.propose("split", {"tasks": [{"title": "one too many"}]},
                            from_task=parent)
        self.assertEqual(self.adjudicate(prop), "rejected")

    def test_an_exhausted_subtree_budget_stops_new_work(self):
        root = self.add_card("root", card_type="t", budget={"usd": 1.0})
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,usd,"
                  "started_at) VALUES ('r_1',?,'build','developer','finished',"
                  "2.0,0)", (root,))
        prop = self.propose("add_task", {"title": "more work"}, from_task=root)
        self.assertEqual(self.adjudicate(prop), "rejected")

    def test_an_unknown_kind_is_refused_at_submission(self):
        with self.assertRaises(ValueError):
            P.submit(self.db, from_task=None, kind="rm_rf", payload={})


class TestAttachment(ProposalCase):
    def test_add_task_attaches_as_a_sibling(self):
        # REGRESSION: adjacent work discovered in passing became a *child* of
        # the card that found it, which then deadlocked behind it.
        parent = self.add_card("epic", card_type="t")
        worker = self.add_card("worker", card_type="t", parent_id=parent)
        prop = self.propose("add_task", {"title": "adjacent work"},
                            from_task=worker)
        self.assertEqual(self.adjudicate(prop), "accepted")
        made = self.db.q1("SELECT id, parent_id FROM tasks WHERE title=?",
                          ("adjacent work",))
        self.assertEqual(made["parent_id"], parent)

    def test_split_attaches_as_a_child_and_blocks_the_original(self):
        worker = self.add_card("worker", card_type="t")
        prop = self.propose("split", {"tasks": [{"title": "piece one"}]},
                            from_task=worker)
        # `split` is an arbiter-tier kind by default; this test is about where
        # the pieces land, so apply it directly.
        P.apply_proposal(self.db, self.root, self.cfg, self.wfs, prop,
                         "arbiter", "test")
        made = self.db.q1("SELECT id, parent_id FROM tasks WHERE title=?",
                          ("piece one",))
        self.assertEqual(made["parent_id"], worker)
        self.assertIn(made["id"], B.deps_of(self.db, worker))

    def test_a_rootless_card_adopts_its_own_proposal(self):
        # keeps the subtree budget meaningful when there is no parent to use
        worker = self.add_card("worker", card_type="t")
        prop = self.propose("add_task", {"title": "follow up"}, from_task=worker)
        self.adjudicate(prop)
        made = self.db.q1("SELECT parent_id FROM tasks WHERE title=?",
                          ("follow up",))
        self.assertEqual(made["parent_id"], worker)

    def test_a_new_card_inherits_the_proposer_card_type(self):
        self.set_workflow("special", [{"stage": "build", "agent": "developer"}])
        worker = self.add_card("worker", card_type="special")
        prop = self.propose("add_task", {"title": "inherited"}, from_task=worker)
        self.adjudicate(prop)
        made = self.db.q1("SELECT card_type FROM tasks WHERE title=?",
                          ("inherited",))
        self.assertEqual(made["card_type"], "special")


class TestDuplicates(ProposalCase):
    def test_a_near_duplicate_merges_instead_of_adding(self):
        self.add_card("Add rate limiting to the public API", card_type="t")
        before = self.db.q1("SELECT COUNT(*) c FROM tasks")["c"]
        prop = self.propose("add_task",
                            {"title": "Add rate limiting to the public API"})
        self.adjudicate(prop)
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM tasks")["c"], before)
        self.assertTrue(self.db.q1(
            "SELECT id FROM events WHERE kind='proposal.merged'"))

    def test_genuinely_different_work_is_not_merged(self):
        self.add_card("Add rate limiting to the public API", card_type="t")
        before = self.db.q1("SELECT COUNT(*) c FROM tasks")["c"]
        prop = self.propose("add_task", {"title": "Rewrite the billing exporter"})
        self.adjudicate(prop)
        self.assertEqual(self.db.q1("SELECT COUNT(*) c FROM tasks")["c"],
                         before + 1)


class TestAutonomyTiers(ProposalCase):
    def test_policy_accepts_routine_additions_without_a_model(self):
        self.set_config(**{"mutation.autonomy": "policy"})
        prop = self.propose("add_task", {"title": "routine work"})
        self.assertEqual(self.adjudicate(prop), "accepted")
        row = self.db.q1("SELECT tier FROM proposals WHERE id=?", (prop["id"],))
        self.assertEqual(row["tier"], "policy")

    def test_human_autonomy_escalates_everything(self):
        self.set_config(**{"mutation.autonomy": "human"})
        tid = self.add_card(card_type="t")
        prop = self.propose("add_task", {"title": "anything"}, from_task=tid)
        self.assertEqual(self.adjudicate(prop), "escalated")
        self.assertEqual(self.task(tid)["status"], B.CHECKPOINT)

    def test_an_urgent_blocker_goes_straight_to_a_human(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("raise_blocker", {"reason": "the schema is wrong"},
                            from_task=tid, urgency="high")
        self.assertEqual(self.adjudicate(prop), "escalated")

    def test_an_unreachable_arbiter_escalates_rather_than_guessing(self):
        # arbiter.command is [] in tests, so the call cannot be made
        self.set_config(**{"mutation.autonomy": "arbiter"})
        tid = self.add_card(card_type="t")
        prop = self.propose("split", {"tasks": [{"title": "piece"}]},
                            from_task=tid)
        self.assertEqual(self.adjudicate(prop), "escalated")

    def test_an_escalated_proposal_carries_its_payload_to_the_checkpoint(self):
        self.set_config(**{"mutation.autonomy": "human"})
        tid = self.add_card(card_type="t")
        prop = self.propose("add_task", {"title": "needs your call"},
                            from_task=tid, rationale="unsure of scope")
        self.adjudicate(prop)
        cp = self.db.q1("SELECT bundle FROM checkpoints WHERE task_id=?", (tid,))
        bundle = json.loads(cp["bundle"])
        self.assertEqual(bundle["payload"]["title"], "needs your call")
        self.assertEqual(bundle["rationale"], "unsure of scope")


class TestBlockerAndDeps(ProposalCase):
    def test_raise_blocker_parks_the_card_with_its_reason(self):
        tid = self.add_card(card_type="t")
        prop = self.propose("raise_blocker",
                            {"reason": "needs a session store first"},
                            from_task=tid)
        self.adjudicate(prop)
        t = self.task(tid)
        self.assertEqual(t["status"], B.BLOCKED)
        self.assertIn("session store", t["block_reason"])

    def test_add_dep_creates_the_edge(self):
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t")
        prop = self.propose("add_dep", {"src": a, "dst": b}, from_task=b)
        self.assertEqual(self.adjudicate(prop), "accepted")
        self.assertIn(a, B.deps_of(self.db, b))


class TestExpansionRatio(ProposalCase):
    def test_quiet_boards_report_no_ratio(self):
        ratio, _, _ = P.expansion_ratio(self.db, self.cfg)
        self.assertEqual(ratio, 0.0)

    def test_creating_far_more_than_you_finish_raises_the_ratio(self):
        self.set_config(**{"containment.expansion_ratio_window": 4})
        for i in range(8):
            self.db.emit("task.created", f"t_{i}")
        self.db.emit("task.done", "t_0")
        ratio, created, done = P.expansion_ratio(self.db, self.cfg)
        # the window is rolling, so exact counts depend on it — the signal is
        # that creation far outpaces completion
        self.assertGreater(created, done)
        self.assertGreater(ratio, 2.5)

    def test_a_board_that_keeps_up_stays_under_the_limit(self):
        self.set_config(**{"containment.expansion_ratio_window": 4})
        for i in range(6):
            self.db.emit("task.created", f"t_{i}")
            self.db.emit("task.done", f"t_{i}")
        ratio, _, _ = P.expansion_ratio(self.db, self.cfg)
        self.assertLessEqual(ratio, 1.0)

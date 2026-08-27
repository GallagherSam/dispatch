"""Cards, edges, readiness, and stage advancement."""
from dispatch import board as B
from tests.helpers import BoardCase


class TestEdges(BoardCase):
    needs_git = False

    def test_a_cycle_is_refused(self):
        a, b = self.add_card("a", start=False), self.add_card("b", start=False)
        B.link(self.db, a, b)
        with self.assertRaises(ValueError):
            B.link(self.db, b, a)

    def test_a_longer_cycle_is_also_refused(self):
        a = self.add_card("a", start=False)
        b = self.add_card("b", start=False)
        c = self.add_card("c", start=False)
        B.link(self.db, a, b)
        B.link(self.db, b, c)
        with self.assertRaises(ValueError):
            B.link(self.db, c, a)

    def test_self_edges_are_ignored(self):
        a = self.add_card("a", start=False)
        self.assertIsNone(B.link(self.db, a, a))

    def test_mutex_edges_may_point_both_ways(self):
        # a mutex is exclusion, not ordering, so it must not be cycle-checked
        a, b = self.add_card("a", start=False), self.add_card("b", start=False)
        self.assertIsNotNone(B.link(self.db, a, b, "mutex"))
        self.assertIsNotNone(B.link(self.db, b, a, "mutex"))

    def test_duplicate_edges_collapse(self):
        a, b = self.add_card("a", start=False), self.add_card("b", start=False)
        B.link(self.db, a, b)
        B.link(self.db, a, b)
        self.assertEqual(len(B.deps_of(self.db, b)), 1)


class TestReadiness(BoardCase):
    needs_git = False

    def test_a_backlog_card_is_not_ready(self):
        tid = self.add_card(start=False)
        self.assertIn("in backlog — not yet started",
                      B.blockers(self.db, self.cfg, self.wfs, self.task(tid)))

    def test_an_unfinished_dependency_blocks(self):
        a = self.add_card("upstream")
        b = self.add_card("downstream")
        B.link(self.db, a, b)
        blockers = B.blockers(self.db, self.cfg, self.wfs, self.task(b))
        self.assertTrue(any(a in x for x in blockers), blockers)
        self.assertNotIn(b, [t["id"] for t in
                             B.ready_set(self.db, self.cfg, self.wfs)])

    def test_a_finished_dependency_unblocks(self):
        a = self.add_card("upstream")
        b = self.add_card("downstream")
        B.link(self.db, a, b)
        B.update(self.db, a, status=B.DONE, stage="done")
        self.assertEqual(B.blockers(self.db, self.cfg, self.wfs, self.task(b)), [])

    def test_children_block_the_parent_only_at_its_final_stage(self):
        # REGRESSION: children blocked a parent at every stage, so a card that
        # proposed follow-up work deadlocked behind the work it spawned.
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "integrate", "agent": "integrator"}])
        parent = self.add_card("parent", card_type="t")
        self.add_card("child", card_type="t", parent_id=parent)

        self.assertStage(parent, "build")
        self.assertEqual(B.blockers(self.db, self.cfg, self.wfs,
                                    self.task(parent)), [])

        B.update(self.db, parent, stage="integrate")
        blockers = B.blockers(self.db, self.cfg, self.wfs, self.task(parent))
        self.assertTrue(any("child card" in b for b in blockers), blockers)

    def test_a_deferred_card_reports_its_reason(self):
        import time
        tid = self.add_card()
        B.update(self.db, tid, defer_until=time.time() + 600,
                 defer_reason="quota_above: quota at 4%")
        blockers = B.blockers(self.db, self.cfg, self.wfs, self.task(tid))
        self.assertTrue(any("quota at 4%" in b for b in blockers), blockers)

    def test_a_lock_held_elsewhere_blocks(self):
        self.only_workflow("t", [{"stage": "integrate", "agent": "integrator",
                                  "lock": "integration"}])
        a = self.add_card("a", card_type="t")
        b = self.add_card("b", card_type="t")
        B.acquire_lock(self.db, "integration", a)
        blockers = B.blockers(self.db, self.cfg, self.wfs, self.task(b))
        self.assertTrue(any("integration" in x for x in blockers), blockers)

    def test_ready_set_is_priority_ordered(self):
        low = self.add_card("low", priority=10)
        high = self.add_card("high", priority=90)
        ids = [t["id"] for t in B.ready_set(self.db, self.cfg, self.wfs)]
        self.assertLess(ids.index(high), ids.index(low))


class TestPipeline(BoardCase):
    needs_git = False

    def test_start_moves_a_card_onto_the_first_stage_with_its_agent(self):
        self.only_workflow("t", [{"stage": "qa", "agent": "qa"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        self.assertStage(tid, "qa", "queued")
        self.assertEqual(self.task(tid)["agent_type"], "qa")

    def test_advance_walks_the_pipeline_then_finishes(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        self.assertEqual(B.advance(self.db, self.cfg, self.wfs, tid), "review")
        self.assertEqual(self.task(tid)["agent_type"], "reviewer")
        self.assertEqual(B.advance(self.db, self.cfg, self.wfs, tid), B.DONE)
        self.assertStage(tid, "done", B.DONE)

    def test_advancing_clears_attempts_and_stale_evidence(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, attempts=2, last_evidence="an old failure")
        B.advance(self.db, self.cfg, self.wfs, tid)
        t = self.task(tid)
        self.assertEqual(t["attempts"], 0)
        self.assertIsNone(t["last_evidence"])

    def test_an_empty_pipeline_blocks_rather_than_silently_finishing(self):
        self.only_workflow("empty", [])
        tid = self.add_card(card_type="empty", start=False)
        B.start_card(self.db, self.wfs, tid)
        t = self.task(tid)
        self.assertEqual(t["status"], B.BLOCKED)
        self.assertIn("empty pipeline", t["block_reason"])


class TestCheckpoints(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])

    def test_approving_a_signoff_advances_the_card(self):
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "sign off?", kind="signoff")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        self.assertStage(tid, "review")

    def test_approving_an_escalation_lets_the_stage_run(self):
        # REGRESSION: approving an escalation used to advance the card, silently
        # skipping the stage it had never reached.
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "no criteria", kind="escalation")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        self.assertStage(tid, "build", B.QUEUED)

    def test_rejecting_a_signoff_sends_it_back_a_stage_with_your_reason(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, stage="review")
        cid = B.open_checkpoint(self.db, tid, "sign off?", kind="signoff")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "reject",
                             note="the backoff needs jitter")
        t = self.task(tid)
        self.assertEqual(t["stage"], "build")
        self.assertIn("jitter", t["last_evidence"])

    def test_rejecting_an_escalation_parks_the_card(self):
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "budget?", kind="escalation")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "reject",
                             note="not worth the spend")
        t = self.task(tid)
        self.assertEqual(t["status"], B.BLOCKED)
        self.assertIn("not worth the spend", t["block_reason"])

    def test_amending_appends_your_note_to_the_brief(self):
        tid = self.add_card(card_type="t", brief="original brief")
        cid = B.open_checkpoint(self.db, tid, "sign off?")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "amend",
                             note="also handle the empty case")
        t = self.task(tid)
        self.assertIn("original brief", t["brief"])
        self.assertIn("also handle the empty case", t["brief"])
        self.assertEqual(t["status"], B.QUEUED)

    def test_a_resolved_checkpoint_cannot_be_resolved_twice(self):
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "sign off?")
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        stage_after_first = self.task(tid)["stage"]
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")
        self.assertEqual(self.task(tid)["stage"], stage_after_first)


class TestSubtrees(BoardCase):
    needs_git = False

    def test_cancel_cascades_to_children(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        grandchild = self.add_card("grandchild", parent_id=child)
        B.cancel(self.db, parent)
        for tid in (parent, child, grandchild):
            self.assertEqual(self.task(tid)["status"], B.CANCELLED)

    def test_cancel_leaves_finished_work_alone(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        B.update(self.db, child, status=B.DONE)
        B.cancel(self.db, parent)
        self.assertEqual(self.task(child)["status"], B.DONE)

    def test_depth_is_measured_from_the_root(self):
        a = self.add_card("a")
        b = self.add_card("b", parent_id=a)
        c = self.add_card("c", parent_id=b)
        self.assertEqual(B.depth_of(self.db, a), 0)
        self.assertEqual(B.depth_of(self.db, b), 1)
        self.assertEqual(B.depth_of(self.db, c), 2)

    def test_budget_is_drawn_from_the_root_and_shared_by_the_subtree(self):
        root = self.add_card("root", budget={"usd": 5.0})
        child = self.add_card("child", parent_id=root)
        cap, spent = B.subtree_budget(self.db, self.cfg, child)
        self.assertEqual(cap["usd"], 5.0)
        self.assertEqual(spent["usd"], 0.0)

        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,usd,"
                  "started_at) VALUES ('r_1',?,'build','developer','finished',"
                  "3.25,0)", (child,))
        _, spent = B.subtree_budget(self.db, self.cfg, root)
        self.assertEqual(spent["usd"], 3.25)

    def test_a_card_with_no_budget_inherits_the_configured_default(self):
        tid = self.add_card()
        cap, _ = B.subtree_budget(self.db, self.cfg, tid)
        self.assertEqual(cap["usd"],
                         self.cfg["containment"]["default_budget"]["usd"])

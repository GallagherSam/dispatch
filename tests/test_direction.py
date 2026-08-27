"""Describing direction instead of writing cards, and shared memory."""
import json
import os
import sys

from dispatch import board as B
from dispatch import gates as G
from dispatch import memory as MEM
from tests.helpers import BoardCase

GOOD_PLAN = {
    "summary": "Add a token bucket, then wire it in.",
    "cards": [
        {"ref": "bucket", "title": "Add a token bucket",
         "brief": "Implement Bucket in src/api/limiter.py.",
         "acceptance": ["pytest tests/test_limiter.py passes"],
         "scope": ["src/api/**"], "card_type": "development",
         "tags": ["api"], "priority": 60},
        {"ref": "wire", "title": "Wire the limiter into the app",
         "brief": "Add it as middleware.",
         "acceptance": ["pytest tests/test_ratelimit.py passes"],
         "scope": ["src/api/**"], "depends_on": ["bucket"]},
    ],
    "risks": ["shared NAT customers"],
    "out_of_scope": ["per-endpoint limits"],
}


class PlanCase(BoardCase):
    def _planner(self, plan):
        probe = os.path.join(self.tmp, "planner.py")
        with open(probe, "w") as f:
            f.write(
                "import json, os, sys\n"
                "sys.stdin.read()\n"
                "plan = json.loads(os.environ['STUB_PLAN'])\n"
                "open(os.environ['DISPATCH_RESULT'], 'w').write(\n"
                "    json.dumps({'summary': 'planned', 'plan': plan}))\n"
                "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0}))\n")
        os.environ["STUB_PLAN"] = json.dumps(plan)
        self.set_config(**{"runner.command": [sys.executable, probe]})

    def _intent(self, text="Rate limiting on the public API"):
        tid = B.create(self.db, self.cfg, self.wfs, title=text[:60], brief=text,
                       card_type="intent", acceptance=["a plan you would approve"])
        B.start_card(self.db, self.wfs, tid)
        return tid

    def _open_checkpoint(self, tid):
        return self.db.q1("SELECT id FROM checkpoints WHERE task_id=? "
                          "AND status='open'", (tid,))


class TestPlanning(PlanCase):
    def test_a_direction_card_reaches_you_with_a_plan(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        t = self.task(tid)
        self.assertEqual(t["status"], B.CHECKPOINT)
        self.assertEqual(len(t["plan"]["cards"]), 2)

    def test_the_checkpoint_carries_the_plan_for_review(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        cp = self.db.q1("SELECT bundle FROM checkpoints WHERE task_id=?", (tid,))
        bundle = json.loads(cp["bundle"])
        self.assertIn("cards", json.dumps(bundle["plan"]))

    def test_approving_creates_the_cards(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        cid = self._open_checkpoint(tid)["id"]
        B.resolve_checkpoint(self.db, self.cfg, self.wfs, cid, "approve")

        titles = [t["title"] for t in B.all_tasks(self.db)]
        self.assertIn("Add a token bucket", titles)
        self.assertIn("Wire the limiter into the app", titles)

    def test_the_dependencies_in_the_plan_become_edges(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        B.resolve_checkpoint(self.db, self.cfg, self.wfs,
                             self._open_checkpoint(tid)["id"], "approve")
        by_title = {t["title"]: t for t in B.all_tasks(self.db)}
        bucket = by_title["Add a token bucket"]["id"]
        wire = by_title["Wire the limiter into the app"]["id"]
        self.assertIn(bucket, B.deps_of(self.db, wire))

    def test_only_the_unblocked_cards_start(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        B.resolve_checkpoint(self.db, self.cfg, self.wfs,
                             self._open_checkpoint(tid)["id"], "approve")
        by_title = {t["title"]: t for t in B.all_tasks(self.db)}
        self.assertNotEqual(by_title["Add a token bucket"]["stage"], "backlog")
        self.assertEqual(by_title["Wire the limiter into the app"]["stage"],
                         "backlog")

    def test_the_new_cards_are_not_children_of_the_direction(self):
        # a parent waits for its children at its final stage, and the
        # direction's final stage is the approval — parenting would deadlock it
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        B.resolve_checkpoint(self.db, self.cfg, self.wfs,
                             self._open_checkpoint(tid)["id"], "approve")
        made = [t for t in B.all_tasks(self.db) if t["card_type"] != "intent"]
        self.assertTrue(made)
        for t in made:
            self.assertIsNone(t["parent_id"])
            self.assertEqual(t["provenance"], f"intent:{tid}")
        self.assertEqual(self.task(tid)["status"], B.DONE)

    def test_they_are_tagged_so_you_can_find_them_together(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        B.resolve_checkpoint(self.db, self.cfg, self.wfs,
                             self._open_checkpoint(tid)["id"], "approve")
        short = tid.split("_")[-1]
        made = [t for t in B.all_tasks(self.db) if t["card_type"] != "intent"]
        for t in made:
            self.assertIn(f"from:{short}", t["tags"])

    def test_amending_sends_it_back_to_be_replanned(self):
        self._planner(GOOD_PLAN)
        tid = self._intent()
        self.drain(until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=200)
        B.resolve_checkpoint(self.db, self.cfg, self.wfs,
                             self._open_checkpoint(tid)["id"], "amend",
                             note="split the middleware card")
        t = self.task(tid)
        self.assertEqual(t["stage"], "spec")
        self.assertIsNone(t["plan"])
        self.assertIn("split the middleware card", t["last_evidence"])
        self.assertEqual(len([x for x in B.all_tasks(self.db)
                              if x["card_type"] != "intent"]), 0)


class TestPlanGate(PlanCase):
    def _verdict(self, plan):
        tid = self._intent()
        B.update(self.db, tid, plan=json.dumps(plan) if plan else None)
        from dispatch.config import paths
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
               "root": self.root, "paths": paths(self.root),
               "task": self.task(tid)}
        return G.BUILTINS["has_plan"](ctx, [])

    def test_a_good_plan_passes(self):
        self.assertEqual(self._verdict(GOOD_PLAN).verdict, G.PASS)

    def test_a_card_with_no_acceptance_is_refused(self):
        bad = json.loads(json.dumps(GOOD_PLAN))
        del bad["cards"][0]["acceptance"]
        v = self._verdict(bad)
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("no acceptance criteria", v.evidence)

    def test_a_dangling_dependency_is_refused(self):
        bad = json.loads(json.dumps(GOOD_PLAN))
        bad["cards"][1]["depends_on"] = ["nonexistent"]
        v = self._verdict(bad)
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("not in the plan", v.evidence)

    def test_no_cards_escalates_rather_than_inventing_work(self):
        v = self._verdict({"summary": "too vague to plan", "cards": []})
        self.assertEqual(v.verdict, G.ESCALATE)
        self.assertIn("too vague", v.evidence)

    def test_a_missing_plan_is_a_failure(self):
        self.assertEqual(self._verdict(None).verdict, G.FAIL)


class TestMemory(BoardCase):
    needs_git = False

    def test_writing_then_finding_it(self):
        MEM.add(self.db, title="Where the API tests live",
                body="tests/api/, run with npm test -- api",
                tags=["api", "testing"], kind="pointer")
        found = MEM.search(self.db, "api tests")
        self.assertTrue(found)
        self.assertEqual(found[0]["title"], "Where the API tests live")

    def test_search_ranks_the_relevant_one_first(self):
        MEM.add(self.db, title="Deploy runbook", body="how to ship a release")
        MEM.add(self.db, title="The limiter is not monotonic-safe",
                body="src/api/limiter.py uses time.time(), a clock jump "
                     "hands out free tokens")
        found = MEM.search(self.db, "limiter clock tokens")
        self.assertEqual(found[0]["title"], "The limiter is not monotonic-safe")

    def test_the_same_title_updates_rather_than_duplicating(self):
        MEM.add(self.db, title="Test command", body="pytest -q")
        MEM.add(self.db, title="test command", body="python3 -m pytest -q")
        rows = MEM.all_memories(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "python3 -m pytest -q")

    def test_filtering_by_tag(self):
        MEM.add(self.db, title="A", body="shared word", tags=["api"])
        MEM.add(self.db, title="B", body="shared word", tags=["docs"])
        found = MEM.search(self.db, "shared word", tags=["docs"])
        self.assertEqual([m["title"] for m in found], ["B"])

    def test_punctuation_in_a_query_does_not_break_search(self):
        # a card title goes straight into the query, and FTS5 syntax is a
        # minefield of operators
        MEM.add(self.db, title="Limiter", body="src/api/limiter.py")
        for q in ['src/api/limiter.py "quoted"', "NOT AND OR", "a* b(c)",
                  "-- ; DROP", "()"]:
            MEM.search(self.db, q)

    def test_deleting(self):
        mid = MEM.add(self.db, title="X", body="y")
        self.assertTrue(MEM.delete(self.db, mid))
        self.assertEqual(MEM.all_memories(self.db), [])
        self.assertFalse(MEM.delete(self.db, mid))

    def test_a_deleted_memory_stops_matching(self):
        mid = MEM.add(self.db, title="Ephemeral", body="quinoa parsnip")
        MEM.delete(self.db, mid)
        self.assertEqual(MEM.search(self.db, "quinoa parsnip"), [])

    def test_editing_reindexes(self):
        mid = MEM.add(self.db, title="Thing", body="aardvark")
        MEM.update(self.db, mid, body="capybara")
        self.assertEqual(MEM.search(self.db, "aardvark"), [])
        self.assertTrue(MEM.search(self.db, "capybara"))


class TestMemoryInPrompts(BoardCase):
    needs_git = False

    def test_relevant_memories_are_injected_into_the_prompt(self):
        # an agent that has to remember to look something up mostly does not
        MEM.add(self.db, title="The limiter is not monotonic-safe",
                body="src/api/limiter.py uses time.time()", tags=["api"],
                kind="gotcha")
        MEM.add(self.db, title="Unrelated trivia", body="the office wifi password")
        tid = self.add_card("Fix the rate limiter refill",
                            brief="src/api/limiter.py hands out free tokens")
        from dispatch import runner as R
        prompt = R.build_prompt(self.db, self.root, self.cfg, self.wfs,
                                self.task(tid), "r_1", "/tmp/r.json")
        self.assertIn("monotonic-safe", prompt)
        self.assertIn("## What earlier agents learned about this repo", prompt)
        self.assertNotIn("wifi password", prompt, "irrelevant memories injected")

    def test_every_prompt_says_how_to_use_memory(self):
        tid = self.add_card("anything")
        from dispatch import runner as R
        prompt = R.build_prompt(self.db, self.root, self.cfg, self.wfs,
                                self.task(tid), "r_1", "/tmp/r.json")
        self.assertIn("dispatch memory search", prompt)
        self.assertIn("dispatch memory add", prompt)

    def test_an_empty_store_adds_no_recall_section(self):
        tid = self.add_card("anything")
        from dispatch import runner as R
        prompt = R.build_prompt(self.db, self.root, self.cfg, self.wfs,
                                self.task(tid), "r_1", "/tmp/r.json")
        self.assertNotIn("## What earlier agents learned about this repo", prompt)
        # but it still says how to write the first one
        self.assertIn("dispatch memory add", prompt)

    def test_injection_is_capped_so_it_cannot_swamp_the_brief(self):
        for i in range(60):
            MEM.add(self.db, title=f"Fact {i} about the limiter",
                    body="x" * 400 + f" limiter {i}")
        tid = self.add_card("limiter work", brief="limiter")
        note = MEM.brief_for(self.db, self.task(tid))
        self.assertLess(len(note), 3000)


class TestMemoryRelevance(BoardCase):
    needs_git = False

    def test_a_memory_sharing_no_real_word_is_not_returned(self):
        # REGRESSION: the query ORs every term, so "the" matched everything
        MEM.add(self.db, title="The limiter is not monotonic-safe",
                body="src/api/limiter.py uses time.time()")
        MEM.add(self.db, title="Unrelated trivia",
                body="the office wifi password is on the whiteboard")
        found = MEM.search(self.db, "Fix the rate limiter refill")
        self.assertEqual([m["title"] for m in found],
                         ["The limiter is not monotonic-safe"])

    def test_stopwords_alone_match_nothing(self):
        MEM.add(self.db, title="Anything", body="some body text here")
        self.assertEqual(MEM.search(self.db, "the and of it is"), [])

    def test_a_real_shared_term_still_matches(self):
        MEM.add(self.db, title="Deploy runbook", body="ship a release with make deploy")
        self.assertTrue(MEM.search(self.db, "how do I deploy"))

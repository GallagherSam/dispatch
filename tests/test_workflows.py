"""Card types, their pipelines, and round-tripping them between repos."""
import json
import os

from dispatch import workflows as W
from dispatch.config import load_agents, paths
from tests.helpers import BoardCase


class TestPipelineQueries(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "qa", "agent": "qa"},
                                 {"stage": "review", "agent": "reviewer"}])

    def test_first_and_next(self):
        self.assertEqual(W.first_stage(self.wfs, "t")["stage"], "build")
        self.assertEqual(W.next_stage(self.wfs, "t", "build")["stage"], "qa")
        self.assertEqual(W.next_stage(self.wfs, "t", "qa")["stage"], "review")

    def test_the_end_of_a_pipeline_is_none(self):
        self.assertIsNone(W.next_stage(self.wfs, "t", "review"))

    def test_an_unknown_card_type_has_no_pipeline(self):
        self.assertEqual(W.pipeline(self.wfs, "nope"), [])
        self.assertIsNone(W.first_stage(self.wfs, "nope"))


class TestValidation(BoardCase):
    needs_git = False

    def _problems(self, stages):
        agents = load_agents(self.root)
        return W.validate({"t": {"label": "t", "stages": stages}},
                          self.cfg, agents)

    def test_a_clean_pipeline_has_no_problems(self):
        self.assertEqual(self._problems(
            [{"stage": "build", "agent": "developer"},
             {"stage": "review", "agent": "reviewer"}]), [])

    def test_an_empty_pipeline_is_flagged(self):
        self.assertTrue(any("never move" in p for p in self._problems([])))

    def test_stages_running_backwards_are_flagged(self):
        problems = self._problems([{"stage": "integrate", "agent": "integrator"},
                                   {"stage": "build", "agent": "developer"}])
        self.assertTrue(any("backwards" in p for p in problems), problems)

    def test_an_unknown_agent_is_flagged(self):
        problems = self._problems([{"stage": "build", "agent": "nobody"}])
        self.assertTrue(any("nobody" in p for p in problems), problems)

    def test_an_unknown_stage_is_flagged(self):
        problems = self._problems([{"stage": "nowhere", "agent": "developer"}])
        self.assertTrue(any("nowhere" in p for p in problems), problems)

    def test_a_repeated_stage_is_flagged(self):
        problems = self._problems([{"stage": "build", "agent": "developer"},
                                   {"stage": "build", "agent": "qa"}])
        self.assertTrue(any("twice" in p for p in problems), problems)


class TestPortability(BoardCase):
    needs_git = False

    def test_export_then_import_round_trips(self):
        self.only_workflow("bespoke", [{"stage": "spec", "agent": "spec",
                                        "gates": ["has_acceptance"]},
                                       {"stage": "signoff", "agent": "human"}])
        path = W.export_file(self.root, self.db)
        self.assertTrue(os.path.exists(path))

        W.save(self.db, {"development": {"label": "d", "stages": []}})
        self.assertNotIn("bespoke", W.load(self.db))

        W.import_file(self.root, self.db, path)
        got = W.load(self.db)
        self.assertIn("bespoke", got)
        self.assertEqual([s["stage"] for s in got["bespoke"]["stages"]],
                         ["spec", "signoff"])
        self.assertEqual(got["bespoke"]["stages"][0]["gates"], ["has_acceptance"])

    def test_import_accepts_a_bare_card_type_map(self):
        with open(os.path.join(self.tmp, "bare.json"), "w") as f:
            json.dump({"docs": {"label": "Docs",
                                "stages": [{"stage": "build",
                                            "agent": "developer"}]}}, f)
        W.import_file(self.root, self.db, os.path.join(self.tmp, "bare.json"))
        self.assertIn("docs", W.load(self.db))

    def test_saving_removes_card_types_that_are_gone(self):
        W.save(self.db, {"a": {"label": "a", "stages": []},
                         "b": {"label": "b", "stages": []}})
        W.save(self.db, {"a": {"label": "a", "stages": []}})
        self.assertEqual(sorted(W.load(self.db)), ["a"])

    def test_init_writes_a_committable_workflows_file(self):
        with open(paths(self.root)["workflows"]) as f:
            payload = json.load(f)
        self.assertIn("card_types", payload)
        self.assertIn("development", payload["card_types"])

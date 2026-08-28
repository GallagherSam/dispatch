"""Choosing which model works a card.

Three places can say, and the most specific wins: the card, then the stage,
then the agent role.
"""
import json
import os
import sys

from dispatch import board as B
from dispatch import workflows as W
from dispatch.config import load_agents, looks_like_a_model
from dispatch.runner import DEFAULT_MODEL, resolve_model
from tests.helpers import BoardCase


class TestPrecedence(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [
            {"stage": "build", "agent": "developer"},
            {"stage": "review", "agent": "reviewer", "model": "opus"},
        ])
        self.agents = load_agents(self.root)

    def _model(self, tid):
        return resolve_model(self.wfs, self.agents, self.task(tid))

    def test_the_role_is_the_floor(self):
        tid = self.add_card(card_type="t")
        self.assertEqual(self._model(tid),
                         self.agents["developer"]["model"])

    def test_a_stage_can_say_this_one_is_worth_the_better_model(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, stage="review", agent_type="reviewer")
        self.assertEqual(self._model(tid), "opus")

    def test_a_card_beats_its_stage(self):
        tid = self.add_card(card_type="t", model="haiku")
        B.update(self.db, tid, stage="review", agent_type="reviewer")
        self.assertEqual(self._model(tid), "haiku")

    def test_a_card_beats_its_role(self):
        tid = self.add_card(card_type="t", model="opus")
        self.assertEqual(self._model(tid), "opus")

    def test_an_unknown_role_still_gets_a_model(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, agent_type="nobody")
        self.assertEqual(self._model(tid), DEFAULT_MODEL)

    def test_clearing_a_card_override_falls_back(self):
        tid = self.add_card(card_type="t", model="opus")
        B.update(self.db, tid, model=None)
        self.assertEqual(self._model(tid), self.agents["developer"]["model"])


class TestItReachesTheAgent(BoardCase):
    def test_the_resolved_model_is_what_gets_run(self):
        probe = os.path.join(self.tmp, "probe.py")
        with open(probe, "w") as f:
            f.write(
                "import json, os, sys\n"
                "sys.stdin.read()\n"
                "open(os.environ['DISPATCH_RESULT'], 'w').write(\n"
                "    json.dumps({'summary': 'model=' + sys.argv[sys.argv.index('--model') + 1]}))\n"
                "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0}))\n")
        self.set_config(**{"runner.command": [sys.executable, probe, "--model", "{model}"]})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "model": "opus"}])
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["summary"], "model=opus")

    def test_a_card_override_reaches_the_agent(self):
        probe = os.path.join(self.tmp, "probe2.py")
        with open(probe, "w") as f:
            f.write(
                "import json, os, sys\n"
                "sys.stdin.read()\n"
                "open(os.environ['DISPATCH_RESULT'], 'w').write(\n"
                "    json.dumps({'summary': sys.argv[sys.argv.index('--model') + 1]}))\n"
                "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0}))\n")
        self.set_config(**{"runner.command": [sys.executable, probe, "--model", "{model}"]})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "model": "sonnet"}])
        tid = self.add_card(card_type="t", model="haiku")
        from dispatch import runner as R
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["summary"], "haiku")

    def test_the_run_records_which_model_worked_it(self):
        # with mixed models, "what did this cost" is only answerable if the
        # run says which one it used
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "model": "opus"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        row = self.db.q1("SELECT model FROM runs WHERE task_id=?", (tid,))
        self.assertEqual(row["model"], "opus")

    def test_the_event_log_records_it_too(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t", model="haiku")
        from dispatch import runner as R
        R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        ev = self.db.q1("SELECT data FROM events WHERE kind='run.started'")
        self.assertEqual(json.loads(ev["data"])["model"], "haiku")


class TestCommandLine(BoardCase):
    needs_git = False

    def test_add_takes_a_model(self):
        rc, out = self.run_cli(["--root", self.root, "add", "A card",
                                "--accept", "ok", "--model", "opus"])
        self.assertEqual(rc, 0)
        tid = out.split()[0]
        self.assertEqual(self.task(tid)["model"], "opus")

    def test_show_says_where_the_model_came_from(self):
        tid = self.add_card("A card", model="opus")
        rc, out = self.run_cli(["--root", self.root, "show", tid])
        self.assertEqual(rc, 0)
        self.assertIn("model=opus", out)
        self.assertIn("from this card", out)

    def test_show_names_the_role_when_nothing_overrides(self):
        tid = self.add_card("A card")
        rc, out = self.run_cli(["--root", self.root, "show", tid])
        self.assertEqual(rc, 0)
        self.assertIn("role)", out)

    def test_edit_can_set_and_clear_it(self):
        tid = self.add_card("A card")
        self.run_cli(["--root", self.root, "edit", tid, "--model", "haiku"])
        self.assertEqual(self.task(tid)["model"], "haiku")
        self.run_cli(["--root", self.root, "edit", tid, "--model", "default"])
        self.assertIsNone(self.task(tid)["model"])

    def test_ls_json_carries_it(self):
        self.add_card("A card", model="opus")
        rc, out = self.run_cli(["--root", self.root, "ls", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out[out.index("["):])[0]["model"], "opus")


class TestValidation(BoardCase):
    needs_git = False

    def test_aliases_and_full_ids_are_accepted(self):
        for name in ("opus", "sonnet", "haiku", "fable",
                     "claude-opus-5", "claude-haiku-4-5-20251001", None, ""):
            self.assertTrue(looks_like_a_model(name), name)

    def test_a_typo_is_flagged_rather_than_failing_inside_a_run(self):
        self.assertFalse(looks_like_a_model("opuss"))
        problems = W.validate(
            {"t": {"label": "t", "stages": [
                {"stage": "build", "agent": "developer", "model": "opuss"}]}},
            self.cfg, load_agents(self.root))
        self.assertTrue(any("opuss" in p for p in problems), problems)

    def test_a_valid_stage_model_raises_nothing(self):
        problems = W.validate(
            {"t": {"label": "t", "stages": [
                {"stage": "build", "agent": "developer", "model": "opus"}]}},
            self.cfg, load_agents(self.root))
        self.assertEqual(problems, [])

    def test_an_unrecognised_name_is_a_warning_not_a_refusal(self):
        # models outlive allowlists; this must not become a gate
        W.save(self.db, {"t": {"label": "t", "stages": [
            {"stage": "build", "agent": "developer", "model": "some-new-model"}]}})
        self.assertEqual(W.load(self.db)["t"]["stages"][0]["model"],
                         "some-new-model")


class TestWebSurface(BoardCase):
    needs_git = False

    def test_the_api_accepts_a_model_on_create(self):
        from dispatch.server import Handler  # noqa: F401  (import smoke)
        tid = B.create(self.db, self.cfg, self.wfs, title="x", model="opus")
        self.assertEqual(self.task(tid)["model"], "opus")

    def test_the_board_offers_the_aliases(self):
        import pathlib
        app = pathlib.Path(__file__).parent.parent / "dispatch/web/app.js"
        js = app.read_text()
        self.assertIn("const MODELS", js)
        for alias in ("opus", "sonnet", "haiku"):
            self.assertIn(f"'{alias}'", js)
        self.assertIn('data-k="model"', js, "no per-stage model in the editor")
        self.assertIn("nModel", js, "no model field on the new-card form")


class TestClearing(BoardCase):
    """"" and None both mean "fall back", and must be stored the same way."""
    needs_git = False

    # REGRESSION: the web form posts "" for a cleared model field, which was
    # stored verbatim. Nothing read it as an override, but `ls --json` and the
    # column disagreed with the CLI's None, so "is this card overridden" had
    # two answers depending on who cleared it.
    def test_an_empty_string_is_stored_as_no_override(self):
        tid = self.add_card("A card", model="opus")
        B.update(self.db, tid, model="")
        self.assertIsNone(self.task(tid)["model"])

    def test_creating_with_an_empty_string_is_no_override(self):
        tid = B.create(self.db, self.cfg, self.wfs, title="x", model="")
        self.assertIsNone(self.task(tid)["model"])

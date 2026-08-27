"""Worktrees, prompt assembly, and reading an agent back."""
import json
import os

from dispatch import board as B
from dispatch import runner as R
from tests.helpers import BoardCase


class TestWorktrees(BoardCase):
    def test_a_worktree_is_created_on_its_own_branch(self):
        tid = self.add_card()
        wt, branch, _base = R.make_worktree(self.root, self.cfg, tid, "build")
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(branch, "dispatch/" + tid)
        self.assertTrue(os.path.exists(os.path.join(wt, "src", "calc.py")))

    def test_reusing_an_existing_branch_does_not_explode(self):
        tid = self.add_card()
        _wt1, _, _ = R.make_worktree(self.root, self.cfg, tid, "build")
        R.remove_worktree(self.root, tid)
        wt2, branch, _ = R.make_worktree(self.root, self.cfg, tid, "qa")
        self.assertTrue(os.path.isdir(wt2))
        self.assertEqual(branch, "dispatch/" + tid)

    def test_build_litter_is_excluded_from_the_worktree(self):
        # REGRESSION: gates run test suites inside the worktree, and the litter
        # they leave was being committed -- inflating diffs and tripping both
        # diff_scope and the small_and_green auto-pass on clean work.
        tid = self.add_card()
        wt, _, base = R.make_worktree(self.root, self.cfg, tid, "build")
        os.makedirs(os.path.join(wt, "__pycache__"), exist_ok=True)
        with open(os.path.join(wt, "__pycache__", "junk.pyc"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(wt, ".pytest_cache"), exist_ok=True)
        with open(os.path.join(wt, ".pytest_cache", "CACHEDIR.TAG"), "w") as f:
            f.write("x")
        with open(os.path.join(wt, "src", "calc.py"), "a") as f:
            f.write("\ndef mul(a, b):\n    return a * b\n")

        R.commit_all(wt, "work")
        _, files = R.diff_against(wt, base)
        self.assertEqual(files, ["src/calc.py"], files)

    def test_the_result_file_is_excluded_too(self):
        tid = self.add_card()
        wt, _, base = R.make_worktree(self.root, self.cfg, tid, "build")
        with open(os.path.join(wt, R.RESULT_FILENAME), "w") as f:
            json.dump({"summary": "x"}, f)
        with open(os.path.join(wt, "src", "calc.py"), "a") as f:
            f.write("\n# touched\n")
        R.commit_all(wt, "work")
        _, files = R.diff_against(wt, base)
        self.assertNotIn(R.RESULT_FILENAME, files)

    def test_commit_all_is_a_noop_when_nothing_changed(self):
        tid = self.add_card()
        wt, _, _ = R.make_worktree(self.root, self.cfg, tid, "build")
        self.assertIsNone(R.commit_all(wt, "nothing to do"))


class TestAgentOutput(BoardCase):
    needs_git = False

    def test_a_single_json_envelope(self):
        summary, usd = R._parse_agent_output(
            json.dumps({"result": "did the thing", "total_cost_usd": 0.42}))
        self.assertEqual(summary, "did the thing")
        self.assertEqual(usd, 0.42)

    def test_a_stream_of_json_lines_takes_the_last(self):
        raw = "\n".join([json.dumps({"type": "assistant"}),
                         json.dumps({"result": "final", "total_cost_usd": 1.5})])
        summary, usd = R._parse_agent_output(raw)
        self.assertEqual(summary, "final")
        self.assertEqual(usd, 1.5)

    def test_plain_text_still_yields_a_summary(self):
        summary, usd = R._parse_agent_output("just some prose")
        self.assertEqual(summary, "just some prose")
        self.assertIsNone(usd)

    def test_empty_output_is_survivable(self):
        self.assertEqual(R._parse_agent_output(""), ("", None))


class TestPromptAssembly(BoardCase):
    needs_git = False

    def _prompt(self, tid):
        return R.build_prompt(self.db, self.root, self.cfg, self.wfs,
                              self.task(tid), "r_test", "/tmp/result.json")

    def test_the_brief_and_criteria_are_present(self):
        tid = self.add_card("A card", brief="do the thing",
                            acceptance=["pytest -q passes"])
        p = self._prompt(tid)
        self.assertIn("do the thing", p)
        self.assertIn("pytest -q passes", p)

    def test_the_next_stage_is_named_so_the_agent_hands_off(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "qa", "agent": "qa"}])
        tid = self.add_card(card_type="t")
        self.assertIn("moves to `qa`", self._prompt(tid))

    def test_the_final_stage_says_so(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        tid = self.add_card(card_type="t")
        self.assertIn("final stage", self._prompt(tid))

    def test_declared_scope_is_stated_as_enforced(self):
        tid = self.add_card(scope=["src/api/**"])
        p = self._prompt(tid)
        self.assertIn("src/api/**", p)
        self.assertIn("rejected by a gate", p)

    def test_a_retry_carries_the_gate_evidence(self):
        tid = self.add_card()
        B.update(self.db, tid, last_evidence="tests_pass: 3 failing assertions")
        p = self._prompt(tid)
        self.assertIn("previous attempt was returned", p)
        self.assertIn("3 failing assertions", p)

    def test_artifact_edges_pull_upstream_context_in(self):
        # context is derived from the graph, never remembered
        up = self.add_card("upstream card")
        down = self.add_card("downstream card")
        B.link(self.db, up, down, "artifact")
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,summary,"
                  "started_at) VALUES ('r_up',?,'build','developer','finished',"
                  "?,0)", (up, "I built the parser and it lives in src/parse.py"))
        p = self._prompt(down)
        self.assertIn("Inputs from upstream cards", p)
        self.assertIn("src/parse.py", p)

    def test_the_parent_brief_is_included_as_framing(self):
        parent = self.add_card("epic", brief="the overall goal")
        child = self.add_card("piece", parent_id=parent)
        p = self._prompt(child)
        self.assertIn("Parent card", p)
        self.assertIn("the overall goal", p)

    def test_the_agent_is_told_how_to_propose_and_not_to_self_complete(self):
        tid = self.add_card()
        p = self._prompt(tid)
        self.assertIn("dispatch propose", p)
        self.assertIn("only a gate may do that", p)
        self.assertIn("Do not commit", p)


class TestLaunch(BoardCase):
    def test_a_full_launch_records_a_run_and_a_diff(self):
        self.plan_agent({"*": {"write": {"src/calc.py":
                                         "def add(a, b):\n    return a + b\n\n"
                                         "def mul(a, b):\n    return a * b\n"},
                               "summary": "added mul", "usd": 0.05}})
        tid = self.add_card()
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["summary"], "added mul")
        self.assertEqual(res["changed_files"], ["src/calc.py"])
        self.assertIn("def mul", res["diff"])
        run = self.db.q1("SELECT * FROM runs WHERE task_id=?", (tid,))
        self.assertEqual(run["usd"], 0.05)
        self.assertEqual(run["status"], "finished")

    def test_the_result_file_is_written_inside_the_worktree(self):
        # REGRESSION: the result path pointed outside the worktree, so a
        # sandboxed agent literally could not report back.
        captured = {}
        real = R.build_prompt

        def spy(db, root, cfg, wfs, task, run_id, result_path):
            captured["path"] = result_path
            return real(db, root, cfg, wfs, task, run_id, result_path)

        R.build_prompt = spy
        try:
            self.plan_agent({"*": {"summary": "ok"}})
            tid = self.add_card()
            res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        finally:
            R.build_prompt = real
        self.assertTrue(captured["path"].startswith(res["cwd"]),
                        f"{captured['path']} is outside {res['cwd']}")
        # and it is copied out, then removed from the tree
        self.assertTrue(os.path.exists(
            os.path.join(res["log_dir"], "result.json")))
        self.assertFalse(os.path.exists(captured["path"]))

    def test_agent_proposals_reach_the_board_as_proposals(self):
        self.plan_agent({"*": {"summary": "found more work",
                               "proposals": [{"kind": "add_task",
                                              "payload": {"title": "extra"},
                                              "rationale": "out of scope"}]}})
        tid = self.add_card()
        R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        prop = self.db.q1("SELECT * FROM proposals WHERE from_task=?", (tid,))
        self.assertIsNotNone(prop)
        self.assertEqual(prop["kind"], "add_task")
        self.assertEqual(prop["status"], "pending")

    def test_a_missing_agent_binary_is_reported_not_raised(self):
        self.set_config(**{"runner.command": ["definitely-not-a-real-binary"]})
        tid = self.add_card()
        res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertNotEqual(res["exit_code"], 0)

    def test_artifacts_reported_by_the_agent_land_on_the_card(self):
        self.plan_agent({"*": {"summary": "done", "artifacts": ["docs/plan.md"]}})
        tid = self.add_card()
        R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertIn("docs/plan.md", self.task(tid)["artifacts"])

"""The command surface — including init, which is how anyone starts."""
import json
import os
import stat

from dispatch import board as B
from dispatch.config import load_config, paths
from tests.helpers import BoardCase, git


class TestInit(BoardCase):
    def test_it_scaffolds_everything_a_board_needs(self):
        p = paths(self.root)
        for key in ("db", "config", "workflows", "agents_json", "settings"):
            self.assertTrue(os.path.exists(p[key]), f"missing {key}")
        for d in ("gates", "agents", "runs", "worktrees"):
            self.assertTrue(os.path.isdir(p[d]), f"missing {d}/")

    def test_agent_prompt_files_are_installed(self):
        agents = os.listdir(paths(self.root)["agents"])
        for role in ("developer.md", "qa.md", "reviewer.md", "spec.md",
                     "integrator.md"):
            self.assertIn(role, agents)

    def test_gate_scripts_are_executable(self):
        gates = paths(self.root)["gates"]
        for fn in os.listdir(gates):
            if fn.endswith(".sh"):
                mode = os.stat(os.path.join(gates, fn)).st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{fn} is not executable")

    def test_board_state_is_gitignored_but_config_is_not(self):
        with open(os.path.join(self.root, ".dispatch", ".gitignore")) as f:
            body = f.read()
        for state in ("board.db", "runs/", "worktrees/", "scheduler.pid"):
            self.assertIn(state, body)
        for config in ("config.json", "workflows.json"):
            self.assertNotIn("\n" + config, body)

    def test_agent_permissions_include_the_detected_test_command(self):
        # an agent that cannot run the tests is writing blind
        with open(paths(self.root)["settings"]) as f:
            settings = json.load(f)
        allow = settings["permissions"]["allow"]
        self.assertTrue(any("pytest" in a for a in allow), allow)
        deny = settings["permissions"]["deny"]
        self.assertTrue(any("git push" in d for d in deny))
        self.assertTrue(any(".env" in d for d in deny))

    def test_a_second_init_refuses_without_force(self):
        rc, out = self.run_cli(["init", self.root])
        self.assertEqual(rc, 1)
        self.assertIn("already initialised", out)

    def test_test_command_detection_prefers_package_json(self):
        import shutil
        import tempfile
        other = tempfile.mkdtemp(prefix="dispatch-node-")
        try:
            with open(os.path.join(other, "package.json"), "w") as f:
                json.dump({"scripts": {"test": "vitest run"}}, f)
            git(other, "init", "-q")
            # --no-verify: this is about detection, not about whether the
            # project's suite actually runs on this machine
            self.run_cli(["init", other, "--no-verify"])
            self.assertEqual(load_config(other)["commands"]["test"], "npm test")
        finally:
            shutil.rmtree(other, ignore_errors=True)


class TestCardCommands(BoardCase):
    needs_git = False

    def test_add_then_show(self):
        rc, out = self.run_cli(["--root", self.root, "add", "A new card",
                                "--brief", "the brief", "--accept", "it works",
                                "--scope", "src/**", "--tag", "x", "--start"])
        self.assertEqual(rc, 0)
        tid = out.split()[0]
        rc, out = self.run_cli(["--root", self.root, "show", tid])
        self.assertEqual(rc, 0)
        self.assertIn("the brief", out)
        self.assertIn("it works", out)

    def test_add_warns_when_there_is_nothing_to_check(self):
        _rc, out = self.run_cli(["--root", self.root, "add", "Vague card"])
        self.assertIn("no acceptance criteria", out)

    def test_add_rejects_an_unknown_card_type(self):
        rc, out = self.run_cli(["--root", self.root, "add", "x",
                                "--type", "nonsense"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown card type", out)

    def test_edit_replaces_and_appends_criteria(self):
        tid = self.add_card(acceptance=["one"])
        self.run_cli(["--root", self.root, "edit", tid, "--accept", "two"])
        self.assertEqual(self.task(tid)["acceptance"], ["two"])
        self.run_cli(["--root", self.root, "edit", tid, "--accept", "three",
                      "--add"])
        self.assertEqual(self.task(tid)["acceptance"], ["two", "three"])

    def test_edit_requeue_clears_a_block(self):
        tid = self.add_card()
        B.update(self.db, tid, status=B.BLOCKED, block_reason="stuck")
        self.run_cli(["--root", self.root, "edit", tid, "--requeue"])
        t = self.task(tid)
        self.assertEqual(t["status"], B.QUEUED)
        self.assertIsNone(t["block_reason"])

    def test_link_refuses_a_cycle_with_a_readable_message(self):
        a, b = self.add_card("a"), self.add_card("b")
        self.run_cli(["--root", self.root, "link", a, b])
        rc, out = self.run_cli(["--root", self.root, "link", b, a])
        self.assertEqual(rc, 1)
        self.assertIn("cycle", out)

    def test_ls_filters_by_stage_and_type(self):
        self.only_workflow("t", [{"stage": "qa", "agent": "qa"}])
        self.add_card("in qa", card_type="t")
        self.add_card("in backlog", card_type="t", start=False)
        _rc, out = self.run_cli(["--root", self.root, "ls", "--stage", "qa"])
        self.assertIn("in qa", out)
        self.assertNotIn("in backlog", out)

    def test_show_on_a_missing_card_fails_cleanly(self):
        rc, out = self.run_cli(["--root", self.root, "show", "t_nope00"])
        self.assertEqual(rc, 1)
        self.assertIn("no such card", out)

    def test_blocked_explains_each_hold(self):
        a, b = self.add_card("upstream"), self.add_card("downstream")
        B.link(self.db, a, b)
        _rc, out = self.run_cli(["--root", self.root, "blocked"])
        self.assertIn(b, out)
        self.assertIn("waits on", out)

    def test_blocked_says_so_when_nothing_is_held(self):
        self.add_card()
        _rc, out = self.run_cli(["--root", self.root, "blocked"])
        self.assertIn("nothing blocked", out)


class TestCheckpointCommands(BoardCase):
    needs_git = False

    def test_needs_lists_open_checkpoints(self):
        tid = self.add_card("a card needing you")
        B.open_checkpoint(self.db, tid, "sign off?")
        _rc, out = self.run_cli(["--root", self.root, "needs"])
        self.assertIn("sign off?", out)

    def test_needs_is_quiet_when_it_is_quiet(self):
        _rc, out = self.run_cli(["--root", self.root, "needs"])
        self.assertIn("nothing waiting on you", out)

    def test_rejecting_without_a_reason_is_refused(self):
        # a rejection with no note gives the next agent nothing to work with
        tid = self.add_card()
        cid = B.open_checkpoint(self.db, tid, "sign off?")
        rc, out = self.run_cli(["--root", self.root, "respond", cid, "reject"])
        self.assertEqual(rc, 1)
        self.assertIn("nothing to work with", out)

    def test_responding_records_the_decision(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "sign off?")
        rc, _ = self.run_cli(["--root", self.root, "respond", cid, "approve"])
        self.assertEqual(rc, 0)
        self.assertStage(tid, "review")


class TestProposeCommand(BoardCase):
    needs_git = False

    def test_an_agent_can_propose_from_the_command_line(self):
        tid = self.add_card()
        rc, out = self.run_cli(["--root", self.root, "propose",
                                "--from", tid, "--kind", "add_task",
                                "--title", "found more work",
                                "--accept", "pytest tests/test_x.py passes",
                                "--rationale", "out of scope here"])
        self.assertEqual(rc, 0)
        self.assertIn("not writing to the board directly", out)
        prop = self.db.q1("SELECT * FROM proposals WHERE from_task=?", (tid,))
        self.assertEqual(json.loads(prop["payload"])["title"], "found more work")

    def test_an_unknown_kind_is_refused_by_the_parser(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_cli(["--root", self.root, "propose", "--kind", "rm_rf"])


class TestStatusAndLog(BoardCase):
    needs_git = False

    def test_status_summarises_the_board(self):
        self.add_card("a card")
        _rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertIn("scheduler", out)
        self.assertIn("cards", out)

    def test_status_surfaces_waiting_checkpoints(self):
        tid = self.add_card()
        B.open_checkpoint(self.db, tid, "sign off?")
        _rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertIn("needs you", out)

    def test_log_shows_the_event_stream(self):
        self.add_card("a card")
        _rc, out = self.run_cli(["--root", self.root, "log"])
        self.assertIn("task.created", out)


class TestWorkflowCommand(BoardCase):
    needs_git = False

    def test_show_renders_every_pipeline(self):
        _rc, out = self.run_cli(["--root", self.root, "workflows"])
        self.assertIn("development", out)
        self.assertIn("integrate", out)

    def test_export_writes_the_file(self):
        rc, _out = self.run_cli(["--root", self.root, "workflows", "export"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(paths(self.root)["workflows"]))

    def test_import_reports_problems_without_refusing(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            json.dump({"card_types": {"broken": {
                "label": "Broken",
                "stages": [{"stage": "build", "agent": "nobody"}]}}}, f)
        rc, out = self.run_cli(["--root", self.root, "workflows", "import",
                                "--file", bad])
        self.assertEqual(rc, 0)
        self.assertIn("nobody", out)


class TestNoBoard(BoardCase):
    needs_git = False

    def test_commands_outside_a_board_say_what_to_do(self):
        import tempfile
        empty = tempfile.mkdtemp(prefix="dispatch-empty-")
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--root", empty, "ls"])
        self.assertIn("dispatch init", str(ctx.exception))

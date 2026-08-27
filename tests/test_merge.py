"""Landing finished cards on the base branch."""
import os

from dispatch import board as B
from dispatch import merge as M
from tests.helpers import BoardCase, git

CALC_WITH_MUL = ("def add(a, b):\n    return a + b\n\n\n"
                 "def mul(a, b):\n    return a * b\n")


class MergeCase(BoardCase):
    def setUp(self):
        super().setUp()
        self.set_config(**{"scheduler.retry_backoff_s": 0.1,
                           "runner.merge_retry_s": 0.1})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["tests_pass"]}])

    def base(self):
        return M.current_branch(self.root)

    def head_files(self):
        return git(self.root, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()

    def file_at_head(self, rel):
        return git(self.root, "show", f"HEAD:{rel}").stdout


class TestSuccessfulMerge(MergeCase):
    def test_a_finished_card_lands_on_the_base_branch(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.assertStage(tid, "done", B.DONE)
        self.assertIn("def mul", self.file_at_head("src/calc.py"))

    def test_the_merge_is_recorded_as_an_event(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.assertTrue(self.db.q1(
            "SELECT id FROM events WHERE kind='task.merged' AND task_id=?", (tid,)))

    def test_the_worktree_and_branch_are_cleaned_up(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        branch = self.task(tid)["workspace"]["branch"]
        self.assertFalse(os.path.isdir(self.task(tid)["workspace"]["worktree"]))
        self.assertNotEqual(
            git(self.root, "rev-parse", "--verify", branch).returncode, 0)

    def test_the_branch_is_kept_when_you_ask_for_it(self):
        self.set_config(**{"runner.delete_branch_after_merge": False})
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        branch = self.task(tid)["workspace"]["branch"]
        self.assertEqual(
            git(self.root, "rev-parse", "--verify", branch).returncode, 0)

    def test_two_cards_both_land(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        a = self.add_card("first", card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.plan_agent({"*": {"write": {
            "src/other.py": "def sub(a, b):\n    return a - b\n"}}})
        b = self.add_card("second", card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.assertStage(a, "done", B.DONE)
        self.assertStage(b, "done", B.DONE)
        self.assertIn("src/other.py", self.head_files())

    def test_merging_can_be_turned_off(self):
        self.set_config(**{"runner.merge_on_done": False})
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.assertStage(tid, "done", B.DONE)
        self.assertNotIn("def mul", self.file_at_head("src/calc.py"))


class TestMergeIsSerialised(MergeCase):
    def test_only_one_card_merges_at_a_time(self):
        self.set_config(**{"scheduler.max_concurrent": 4})
        sched = self.scheduler()
        for i in range(3):
            tid = self.add_card(f"card {i}", card_type="t")
            B.update(self.db, tid, status=B.MERGING, stage="done",
                     workspace={"branch": f"dispatch/{tid}", "worktree": None,
                                "base_ref": self.base()})
        sched.start_merge()
        first = sched._merging
        sched.start_merge()
        self.assertEqual(sched._merging, first)
        if sched._merge_thread:
            sched._merge_thread.join(timeout=10)


class TestMergeFailure(MergeCase):
    def _finish_without_landing(self, tid):
        """Run the pipeline with merging off, then hand the card to the merge
        worker deliberately — so the base branch can be changed in between
        without racing it."""
        self.set_config(**{"runner.merge_on_done": False})
        self.drain(max_ticks=200)
        self.assertStage(tid, "done", B.DONE)
        self.set_config(**{"runner.merge_on_done": True,
                           "runner.merge_retry_s": 0.1})
        B.update(self.db, tid, status=B.MERGING)

    def test_a_card_that_breaks_the_base_branch_does_not_land(self):
        # the point of re-running the gates after rebasing: passing in
        # isolation says nothing about passing on top of what landed since
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self._finish_without_landing(tid)

        # someone lands a change that the card's tests do not survive
        self.write("tests/test_calc.py",
                   "def test_impossible():\n    assert False\n")
        git(self.root, "add", "tests/test_calc.py")
        git(self.root, "commit", "-qm", "a failing test on the base branch")

        sched = self.scheduler()
        self.drain(sched, until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=300)
        t = self.task(tid)
        self.assertEqual(t["status"], B.CHECKPOINT)
        self.assertIn("could not land", t["block_reason"])
        self.assertIn("tests_pass", t["last_evidence"])
        self.assertTrue(self.db.q1(
            "SELECT id FROM checkpoints WHERE task_id=? AND status='open'", (tid,)))
        self.assertNotIn("def mul", self.file_at_head("src/calc.py"))

    def test_a_rebase_conflict_is_reported_not_forced(self):
        self.plan_agent({"*": {"write": {
            "src/calc.py": "def add(a, b):\n    return a + b  # from the card\n"}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self._finish_without_landing(tid)

        self.write("src/calc.py",
                   "def add(a, b):\n    return a + b  # from the base branch\n")
        git(self.root, "add", "src/calc.py")
        git(self.root, "commit", "-qm", "conflicting edit")

        sched = self.scheduler()
        self.drain(sched, until=lambda s: s.task(tid)["status"] == B.CHECKPOINT,
                   max_ticks=300)
        self.assertEqual(self.task(tid)["status"], B.CHECKPOINT)
        self.assertIn("conflict", self.task(tid)["last_evidence"].lower())
        # the base branch is untouched -- nothing was forced
        self.assertIn("from the base branch", self.file_at_head("src/calc.py"))

    def test_untracked_files_do_not_block_a_merge(self):
        # `dispatch init` leaves .dispatch/ untracked; that must not stop work
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        self.write("some-scratch-file.txt", "not in git\n")
        tid = self.add_card(card_type="t", scope=["src/**"])
        self.drain(max_ticks=200)
        self.assertStage(tid, "done", B.DONE)

    def test_a_dirty_repo_defers_rather_than_failing_the_card(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self._finish_without_landing(tid)
        sched = self.scheduler()

        # a real half-finished edit to a tracked file -- untracked files are
        # irrelevant to a fast-forward and must not block one
        self.write("src/calc.py", "def add(a, b):\n    return a + b  # WIP\n")
        for _ in range(6):
            sched.tick()
            if sched._merge_thread:
                sched._merge_thread.join(timeout=10)
        t = self.task(tid)
        self.assertEqual(t["status"], B.MERGING, "a dirty repo is not the card's fault")
        self.assertIn("merge", (t["defer_reason"] or ""))

        # tidy up and it lands
        git(self.root, "checkout", "--", "src/calc.py")
        B.update(self.db, tid, defer_until=0)
        self.drain(sched, until=lambda s: s.task(tid)["status"] == B.DONE,
                   max_ticks=200)
        self.assertIn("def mul", self.file_at_head("src/calc.py"))

    def test_dispatch_leaves_your_checked_out_branch_alone(self):
        self.plan_agent({"*": {"write": {"src/calc.py": CALC_WITH_MUL}}})
        tid = self.add_card(card_type="t", scope=["src/**"])
        self._finish_without_landing(tid)
        sched = self.scheduler()
        git(self.root, "checkout", "-q", "-b", "my-own-work")
        for _ in range(4):
            sched.tick()
            if sched._merge_thread:
                sched._merge_thread.join(timeout=10)
        self.assertEqual(self.task(tid)["status"], B.MERGING)
        self.assertEqual(M.current_branch(self.root), "my-own-work")


class TestMergePrimitives(MergeCase):
    def test_base_branch_prefers_an_explicit_setting(self):
        self.set_config(**{"runner.base_branch": "release"})
        task = {"workspace": {"base_ref": "main"}}
        self.assertEqual(M.base_branch(self.cfg, task, self.root), "release")

    def test_base_branch_otherwise_uses_what_the_card_branched_from(self):
        task = {"workspace": {"base_ref": "main"}}
        self.assertEqual(M.base_branch(self.cfg, task, self.root), "main")

    def test_a_card_with_no_branch_is_skipped_not_failed(self):
        tid = self.add_card(card_type="t")
        outcome, _ = M.merge_card(self.db, self.root, self.cfg, self.wfs,
                                  self.task(tid))
        self.assertEqual(outcome, M.SKIPPED)

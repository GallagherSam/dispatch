"""Fixes from the 2026-08-29 field report.

The theme of that session: the pipeline's thinking stages were excellent and
its acting stages were blocked. Each test here pins one of those.
"""
import os
import shutil
import subprocess
import tempfile

from dispatch import board as B
from dispatch import gates as G
from dispatch import runner as R
from dispatch.config import DEFAULT_AGENTS
from tests.helpers import BoardCase, git


class TestTheReviewerCanRunTheSuite(BoardCase):
    needs_git = False

    # REGRESSION: the reviewer's shell was restricted to `Bash(git *)`, so the
    # stage whose whole job is verification could not run the test command.
    # Every handback for a session said "I could not execute the suite", and
    # the operator ran the gate on every card by hand.
    def test_the_reviewer_is_not_restricted_to_git(self):
        tools = DEFAULT_AGENTS["reviewer"]["allowed_tools"]
        self.assertIn("Bash", tools)
        self.assertNotIn("Bash(git *)", tools,
                         "the reviewer still cannot run the test command")

    def test_the_reviewer_still_may_not_author(self):
        # what keeps the role honest is the absence of Write and Edit, not the
        # absence of a shell
        tools = DEFAULT_AGENTS["reviewer"]["allowed_tools"].split(",")
        self.assertNotIn("Write", tools)
        self.assertNotIn("Edit", tools)


class TestTheIntegratorIsToldTheTruth(BoardCase):
    needs_git = False

    # REGRESSION: step 1 of the shipped integrator prompt was "Rebase this
    # worktree's branch onto the base branch", which the default sandbox makes
    # impossible — a worktree's git metadata lives in the main repo's
    # .git/worktrees/, outside the writable region. Agents spent real money
    # discovering that and hand-materialising a merge that was thrown away.
    def test_it_does_not_ask_for_an_impossible_rebase(self):
        import pathlib
        md = (pathlib.Path(__file__).parent.parent /
              "dispatch/scaffold/agents/integrator.md").read_text().lower()
        self.assertIn("you do not perform the merge", md)
        self.assertNotIn("1. rebase this worktree", md)


class TestTheConstraintBehindThatPrompt(BoardCase):
    """The structural fact the integrator prompt now describes."""

    def test_a_worktrees_git_metadata_lives_outside_the_worktree(self):
        # This is why `git commit` and `git rebase` fail inside a sandboxed
        # worktree: the sandbox makes the worktree writable, and git needs to
        # write somewhere else entirely. Asserted rather than assumed, so the
        # prompt stops being true loudly rather than quietly.
        wt = os.path.join(self.tmp, "wt")
        git(self.root, "worktree", "add", "-q", wt, "-b", "card/meta")
        with open(os.path.join(wt, ".git")) as f:
            gitdir = f.read().split(":", 1)[1].strip()
        self.assertFalse(
            os.path.realpath(gitdir).startswith(os.path.realpath(wt) + os.sep),
            "git metadata is inside the worktree — the prompt is now wrong")

    def test_git_commit_really_does_fail_when_only_the_worktree_is_writable(self):
        from dispatch import sandbox as SB
        if not SB.backend_available("seatbelt"):
            self.skipTest("needs a real sandbox backend")
        # NOT under $TMPDIR: that whole tree is on the writable list, so a
        # fixture built there can commit happily and proves the opposite of
        # what it looks like it proves. Same trap `test_sandbox_real` exists
        # to avoid.
        outside = tempfile.mkdtemp(dir="/tmp", prefix="dispatch-meta-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        repo = os.path.join(outside, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t.t")
        git(repo, "config", "user.name", "t")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("one\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "initial")
        wt = os.path.join(outside, "wt")
        git(repo, "worktree", "add", "-q", wt, "-b", "card/meta2")

        argv, _meta = SB.wrap({"sandbox": {"enabled": True,
                                           "backend": "seatbelt"}},
                              ["sh", "-c", "echo x >> a.txt && git add -A && "
                                           "git commit -qm inside"],
                              wt, outside)
        out = subprocess.run(argv, cwd=wt, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0,
                            "a sandboxed worktree could commit after all")
        self.assertIn("not permitted", out.stderr.lower())


class TestATruncatedRunIsNotAFailingOne(BoardCase):
    needs_git = False

    # REGRESSION: when credentials expired mid-suite the card came back as
    # "tests failed (exit 1)" over evidence that was a wall of passes and no
    # failure anywhere. An operator who believes that sends good work back.
    def test_passes_with_no_failure_and_nonzero_exit_is_called_out(self):
        note = G._looks_truncated("PASS a\nPASS b\nPASS c\n", 1)
        self.assertTrue(note)
        self.assertIn("cut short", note)

    def test_a_signal_kill_is_called_out(self):
        for rc in (-9, 137, 143):
            self.assertIn("killed by a signal", G._looks_truncated("PASS a\n", rc))

    def test_a_real_failure_is_left_alone(self):
        self.assertEqual(G._looks_truncated("PASS a\nFAIL b\n", 1), "")
        self.assertEqual(G._looks_truncated("ok\nTraceback (most recent call "
                                            "last):\n", 1), "")

    def test_output_with_nothing_useful_is_left_alone(self):
        # a compile error names no test at all; calling that inconclusive
        # would be the expensive direction
        self.assertEqual(G._looks_truncated("error: cannot find symbol\n", 1), "")
        self.assertEqual(G._looks_truncated("", 1), "")

    def test_the_gate_says_so_in_its_reason(self):
        self.set_config(**{"commands.test": "echo PASS a; echo PASS b; exit 1"})
        tid = self.add_card()
        v = G.BUILTINS["tests_pass"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(tid), "cwd": self.root}, [])
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("cut short", v.reason)
        self.assertIn("cut short", v.evidence)


class TestTheAgentIsToldWhatLandedWithoutIt(BoardCase):

    # REGRESSION: a card branches when it starts and reviews an hour later,
    # by which time siblings have merged. Reviewers reported real work as
    # broken — "that ruling does not exist", "the payoff doesn't show" —
    # because from where they stood both were true.
    def test_a_stale_branch_is_named_in_the_brief(self):
        wt = os.path.join(self.tmp, "wt")
        git(self.root, "worktree", "add", "-q", wt, "-b", "card/x")
        base = git(self.root, "rev-parse", "--abbrev-ref",
                   "HEAD").stdout.strip() if hasattr(
            git(self.root, "rev-parse", "--abbrev-ref", "HEAD"), "stdout") else "master"
        with open(os.path.join(self.root, "dodge.md"), "w") as f:
            f.write("the ruling\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "053: add the dodge ruling")

        note = R._staleness_note(self.root, {"merge": {"base_branch": base}},
                                 {"id": "t_x", "workspace": {"path": wt}})
        self.assertIn("behind", note)
        self.assertIn("dodge.md", note)
        self.assertIn("053", note)
        self.assertIn("Do not report those absences as defects", note)

    def test_an_up_to_date_branch_gets_no_note(self):
        wt = os.path.join(self.tmp, "wt2")
        git(self.root, "worktree", "add", "-q", wt, "-b", "card/y")
        base = "master"
        self.assertEqual(
            R._staleness_note(self.root, {"merge": {"base_branch": base}},
                              {"id": "t_y", "workspace": {"path": wt}}), "")

    def test_a_card_with_no_worktree_is_not_an_error(self):
        self.assertEqual(
            R._staleness_note(self.root, {}, {"id": "t_z", "workspace": {}}), "")


class TestWhyACardCannotLandIsVisible(BoardCase):
    needs_git = False

    # REGRESSION: the reason a merge was deferred was reachable only through
    # `dispatch blocked`. `ls` and `status` — what an operator actually runs —
    # showed `merging` and nothing else, which made "why has nothing moved"
    # the most expensive question on the board.
    def _stuck_card(self):
        tid = self.add_card("Card that cannot land")
        B.update(self.db, tid, stage="integrate", status=B.MERGING,
                 defer_reason="merge: base branch has uncommitted changes")
        return tid

    def test_ls_says_why(self):
        tid = self._stuck_card()
        rc, out = self.run_cli(["--root", self.root, "ls"])
        self.assertEqual(rc, 0)
        self.assertIn("cannot land", out)
        self.assertIn("uncommitted changes", out)
        self.assertIn(tid, out)

    def test_status_says_why(self):
        self._stuck_card()
        rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertEqual(rc, 0)
        self.assertIn("cannot land", out)
        self.assertIn("uncommitted changes", out)

    def test_a_card_merging_normally_is_not_reported_as_stuck(self):
        tid = self.add_card("Landing fine")
        B.update(self.db, tid, stage="integrate", status=B.MERGING)
        rc, out = self.run_cli(["--root", self.root, "ls"])
        self.assertEqual(rc, 0)
        self.assertNotIn("cannot land", out)

    def test_ls_rows_are_still_parseable(self):
        # the id must stay the first field of a card row
        self._stuck_card()
        rc, out = self.run_cli(["--root", self.root, "ls"])
        self.assertEqual(rc, 0)
        rows = [ln for ln in out.splitlines() if ln.startswith("t_")]
        self.assertTrue(rows)
        for row in rows:
            self.assertRegex(row.split()[0], r"^t_\w+$")


class TestCancelRecordsWhy(BoardCase):
    needs_git = False

    def test_the_reason_lands_on_the_card_and_in_the_log(self):
        tid = self.add_card("superseded work")
        rc, _ = self.run_cli(["--root", self.root, "cancel", tid,
                              "--reason", "superseded by t_ahnp2b"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task(tid)["status"], B.CANCELLED)
        self.assertEqual(self.task(tid)["block_reason"], "superseded by t_ahnp2b")
        ev = self.db.q1("SELECT data FROM events WHERE kind='task.cancelled'")
        self.assertIn("superseded by t_ahnp2b", ev["data"])

    def test_cancelling_without_a_reason_still_works(self):
        tid = self.add_card("no reason given")
        rc, _ = self.run_cli(["--root", self.root, "cancel", tid])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task(tid)["status"], B.CANCELLED)

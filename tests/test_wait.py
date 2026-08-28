"""Blocking on the board from outside it, and reporting back into a session."""
import json
import threading
import time
from unittest import mock

from dispatch import board as B
from dispatch import watch as W
from tests.helpers import BoardCase


class TestTargets(BoardCase):
    needs_git = False

    def test_explicit_ids_win(self):
        a, _b = self.add_card("a"), self.add_card("b")
        self.assertEqual(W.targets(self.db, [a]), [a])

    def test_no_ids_means_everything_unfinished(self):
        a, b = self.add_card("a"), self.add_card("b")
        B.update(self.db, b, status=B.DONE)
        self.assertEqual(W.targets(self.db), [a])

    def test_filtering_by_tag_and_type(self):
        a = self.add_card("a", tags=["api"])
        self.add_card("b", tags=["web"])
        self.assertEqual(W.targets(self.db, tag="api"), [a])
        self.assertEqual(W.targets(self.db, card_type="bugfix"), [])


class TestOutcome(BoardCase):
    needs_git = False

    def test_unfinished_work_reports_not_yet(self):
        a = self.add_card("a")
        code, _ = W.outcome(self.db, [a])
        self.assertEqual(code, -1)

    def test_all_done_is_success(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DONE)
        code, reason = W.outcome(self.db, [a])
        self.assertEqual(code, W.OK)
        self.assertIn("done", reason)

    def test_a_dead_lettered_card_is_a_failure(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DEADLETTER)
        code, reason = W.outcome(self.db, [a])
        self.assertEqual(code, W.FAILED)
        self.assertIn(a, reason)

    def test_a_checkpoint_returns_rather_than_waiting_forever(self):
        # otherwise a session blocks on something only a human can move
        a = self.add_card("a")
        B.open_checkpoint(self.db, a, "sign off?")
        code, reason = W.outcome(self.db, [a])
        self.assertEqual(code, W.NEEDS_HUMAN)
        self.assertIn("sign off", reason)

    def test_checkpoints_can_be_waited_through_deliberately(self):
        a = self.add_card("a")
        B.open_checkpoint(self.db, a, "sign off?")
        code, _ = W.outcome(self.db, [a], stop_on_checkpoint=False)
        self.assertEqual(code, -1)

    def test_a_checkpoint_on_another_card_is_not_our_business(self):
        a, b = self.add_card("a"), self.add_card("b")
        B.update(self.db, a, status=B.DONE)
        B.open_checkpoint(self.db, b, "unrelated")
        code, _ = W.outcome(self.db, [a])
        self.assertEqual(code, W.OK)


class TestWait(BoardCase):
    needs_git = False

    def test_it_returns_as_soon_as_the_card_lands(self):
        a = self.add_card("a")

        def finish():
            time.sleep(0.3)
            B.update(self.db, a, status=B.DONE)

        threading.Thread(target=finish, daemon=True).start()
        started = time.time()
        code, _ = W.wait(self.db, [a], interval=0.05, timeout=10)
        self.assertEqual(code, W.OK)
        self.assertLess(time.time() - started, 5)

    def test_it_times_out_rather_than_hanging(self):
        a = self.add_card("a")
        code, reason = W.wait(self.db, [a], interval=0.05, timeout=0.3)
        self.assertEqual(code, W.TIMEOUT)
        self.assertIn(a, reason)

    def test_transitions_are_reported_as_they_happen(self):
        # a caller watching the output should learn *what* changed
        a = self.add_card("a")
        seen = []

        def finish():
            time.sleep(0.2)
            B.update(self.db, a, stage="qa")
            time.sleep(0.2)
            B.update(self.db, a, status=B.DONE)

        threading.Thread(target=finish, daemon=True).start()
        W.wait(self.db, [a], interval=0.05, timeout=10,
               on_change=lambda tid, was, now: seen.append(now["status"]))
        self.assertIn(B.DONE, seen)

    def test_an_already_finished_card_returns_at_once(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DONE)
        started = time.time()
        code, _ = W.wait(self.db, [a], interval=5, timeout=30)
        self.assertEqual(code, W.OK)
        self.assertLess(time.time() - started, 1)


class TestWaitCommand(BoardCase):
    def test_it_waits_for_a_real_pipeline_and_reports_success(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        sched = self.scheduler()
        threading.Thread(target=lambda: self.drain(sched, max_ticks=200),
                         daemon=True).start()
        rc, out = self.run_cli(["--root", self.root, "wait", tid,
                                "--timeout", "60", "--interval", "0.2"])
        self.assertEqual(rc, W.OK, out)
        self.assertIn("done", out)

    def test_nothing_to_wait_for_is_success_not_an_error(self):
        rc, out = self.run_cli(["--root", self.root, "wait", "--timeout", "5"])
        self.assertEqual(rc, W.OK)
        self.assertIn("nothing to wait for", out)

    def test_json_output_carries_the_final_state(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DONE)
        _rc, out = self.run_cli(["--root", self.root, "wait", a, "--json"])
        body = json.loads(out[out.index("{"):])
        self.assertEqual(body["exit"], W.OK)
        self.assertEqual(body["cards"][a]["status"], B.DONE)

    def test_a_checkpoint_exits_with_its_own_code(self):
        a = self.add_card("a")
        B.open_checkpoint(self.db, a, "sign off on this?")
        rc, out = self.run_cli(["--root", self.root, "wait", a,
                                "--timeout", "5", "--interval", "0.1"])
        self.assertEqual(rc, W.NEEDS_HUMAN)
        self.assertIn("sign off", out)

    def test_a_failure_exits_nonzero(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DEADLETTER)
        rc, _ = self.run_cli(["--root", self.root, "wait", a, "--timeout", "5"])
        self.assertEqual(rc, W.FAILED)


class TestBoardSummary(BoardCase):
    needs_git = False

    def test_an_idle_board_says_so(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DONE)
        s = W.board_summary(self.db)
        self.assertTrue(s["idle"])
        self.assertIn("idle", W.summary_text(s))

    def test_active_cards_are_listed_with_their_stage(self):
        self.add_card("a card in flight")
        s = W.board_summary(self.db)
        self.assertFalse(s["idle"])
        text = W.summary_text(s)
        self.assertIn("a card in flight", text)

    def test_checkpoints_come_with_the_command_to_answer_them(self):
        a = self.add_card("a")
        B.open_checkpoint(self.db, a, "sign off?")
        text = W.summary_text(W.board_summary(self.db))
        self.assertIn("dispatch respond", text)


class TestStopHook(BoardCase):
    needs_git = False

    def _hook(self, argv, session="s1"):
        import contextlib
        import io
        import sys
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"session_id": session,
                                            "hook_event_name": "Stop"}))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                from dispatch.cli import main
                rc = main(argv)
            return rc, buf.getvalue(), err.getvalue()
        finally:
            sys.stdin = stdin

    def test_it_reports_the_board_without_obstructing_by_default(self):
        self.add_card("a card in flight")
        rc, out, _ = self._hook(["--root", self.root, "hook", "stop"])
        self.assertEqual(rc, 0, "the default must never block a session")
        body = json.loads(out)
        self.assertIn("a card in flight",
                      body["hookSpecificOutput"]["additionalContext"])

    def test_blocking_mode_holds_the_session_while_work_is_in_flight(self):
        self.add_card("still going")
        rc, _, err = self._hook(["--root", self.root, "hook", "stop",
                                 "--block-while-busy"])
        self.assertEqual(rc, 2, "exit 2 is what keeps Claude going")
        self.assertIn("still going", err)

    def test_blocking_mode_lets_go_once_the_board_is_idle(self):
        a = self.add_card("a")
        B.update(self.db, a, status=B.DONE)
        rc, out, _ = self._hook(["--root", self.root, "hook", "stop",
                                 "--block-while-busy"])
        self.assertEqual(rc, 0)
        self.assertIn("idle", out)

    def test_it_gives_up_rather_than_spinning_forever(self):
        # Claude Code passes no loop guard for Stop hooks, so this is ours
        self.add_card("never finishes")
        for _ in range(3):
            rc, _, _ = self._hook(["--root", self.root, "hook", "stop",
                                   "--block-while-busy", "--max-blocks", "3"])
            self.assertEqual(rc, 2)
        rc, out, _ = self._hook(["--root", self.root, "hook", "stop",
                                 "--block-while-busy", "--max-blocks", "3"])
        self.assertEqual(rc, 0, "it must let go eventually")
        self.assertIn("stopped holding this session open", out)

    def test_the_guard_is_per_session(self):
        self.add_card("in flight")
        for _ in range(3):
            self._hook(["--root", self.root, "hook", "stop",
                        "--block-while-busy", "--max-blocks", "3"], session="a")
        rc, _, _ = self._hook(["--root", self.root, "hook", "stop",
                               "--block-while-busy", "--max-blocks", "3"],
                              session="b")
        self.assertEqual(rc, 2, "one session's count must not exhaust another's")

    def test_the_count_resets_once_the_board_goes_idle(self):
        a = self.add_card("a")
        for _ in range(2):
            self._hook(["--root", self.root, "hook", "stop",
                        "--block-while-busy", "--max-blocks", "3"])
        B.update(self.db, a, status=B.DONE)
        self._hook(["--root", self.root, "hook", "stop", "--block-while-busy"])
        B.update(self.db, a, status=B.QUEUED)
        rc, _, _ = self._hook(["--root", self.root, "hook", "stop",
                               "--block-while-busy", "--max-blocks", "3"])
        self.assertEqual(rc, 2)

    def test_no_board_never_obstructs_a_session(self):
        import tempfile
        empty = tempfile.mkdtemp(prefix="dispatch-nohook-")
        rc, _, _ = self._hook(["--root", empty, "hook", "stop",
                               "--block-while-busy"])
        self.assertEqual(rc, 0)


class TestFinalTransitionIsReported(BoardCase):
    needs_git = False

    def test_the_transition_that_ends_the_wait_is_reported(self):
        # REGRESSION: outcome() was checked before the snapshot was diffed, so
        # the change that ended the wait — the one you most want to see — was
        # never handed to the caller.
        a = self.add_card("a")
        B.update(self.db, a, stage="qa")
        seen = []
        W.wait(self.db, [a], interval=0.01, timeout=5,
               on_change=lambda tid, was, now: seen.append(now["status"]))
        # already terminal? no — make it terminal mid-flight
        self.assertTrue(seen)

    def test_a_card_finishing_between_polls_is_still_reported(self):
        import threading
        a = self.add_card("a")
        seen = []

        def finish():
            time.sleep(0.15)
            B.update(self.db, a, status=B.DONE)

        threading.Thread(target=finish, daemon=True).start()
        code, _ = W.wait(self.db, [a], interval=0.05, timeout=10,
                         on_change=lambda tid, was, now: seen.append(now["status"]))
        self.assertEqual(code, W.OK)
        self.assertIn(B.DONE, seen, "the finishing transition went unreported")

    def test_it_is_reported_every_time_not_just_usually(self):
        import threading
        for _ in range(12):
            a = self.add_card("a")
            seen = []

            # The writer runs in a daemon thread nobody joins, so an
            # exception in it used to vanish and this test would fail as
            # "the transition was not reported" when the transition had
            # never happened at all. Two different bugs, one message.
            err: list[BaseException] = []

            def finish(tid=a, err=err):
                try:
                    time.sleep(0.03)
                    B.update(self.db, tid, status=B.DONE)
                except BaseException as e:
                    err.append(e)

            th = threading.Thread(target=finish, daemon=True)
            th.start()
            W.wait(self.db, [a], interval=0.02, timeout=5,
                   on_change=lambda t, w, n, seen=seen: seen.append(n["status"]))
            th.join(timeout=5)
            self.assertFalse(err, f"the writer thread raised: {err}")
            self.assertEqual(self.task(a)["status"], B.DONE,
                             "the card never actually reached done")
            self.assertIn(B.DONE, seen, f"transition unreported; saw {seen}")
            B.cancel(self.db, a)


class TestTheEndingTransitionIsNeverLost(BoardCase):
    """The gap between reporting and deciding.

    Reproduced on CI as a one-in-many flake before it was understood: the card
    reached done, the writer raised nothing, and `wait` returned OK having
    reported only 'queued'.
    """
    needs_git = False

    # REGRESSION: `wait` reported from one snapshot and then called `outcome`,
    # which took its own. A card that finished between the two reads was
    # returned as done and reported as nothing — so a session waiting on the
    # board learned the wait had ended but never that the card landed. This
    # forces that interleaving instead of waiting for a slow machine to.
    def test_a_card_finishing_between_the_two_reads_is_still_reported(self):
        a = self.add_card("a")
        seen = []
        calls = []
        real = W.snapshot

        def racing_snapshot(db, ids):
            state = real(db, ids)
            # exactly once, after the loop has taken its snapshot, land the
            # card — the position the old code read it from and never told
            # anyone about
            calls.append(1)
            if len(calls) == 2:
                B.update(db, a, status=B.DONE)
            return state

        with mock.patch.object(W, "snapshot", racing_snapshot):
            code, _ = W.wait(self.db, [a], interval=0.01, timeout=5,
                             on_change=lambda t, w, n: seen.append(n["status"]))

        self.assertEqual(code, W.OK)
        self.assertEqual(self.task(a)["status"], B.DONE)
        self.assertIn(B.DONE, seen,
                      f"the transition that ended the wait went unreported; saw {seen}")

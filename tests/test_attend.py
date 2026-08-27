"""A session sitting in the loop instead of a person.

The friction this removes: the operator coming back every few minutes to answer
small decisions the session that seeded the cards could have made instantly.
"""
import json
import time

from dispatch import board as B
from dispatch import watch as W
from tests.helpers import BoardCase


class AttendCase(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "signoff", "agent": "human"}])

    def cp(self, tid, topic, **kw):
        return B.open_checkpoint(self.db, tid, kw.pop("question", "decide"),
                                 topic=topic, cfg=self.cfg, **kw)


class TestAudience(AttendCase):
    def test_a_signoff_is_a_sessions_to_make(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "signoff")
        row = self.db.q1("SELECT audience FROM checkpoints WHERE task_id=?", (tid,))
        self.assertEqual(row["audience"], "session")

    def test_secrets_money_and_runaway_are_not(self):
        for topic in ("no_secrets", "budget_remaining", "expansion", "plan"):
            tid = self.add_card(f"card for {topic}", card_type="t")
            self.cp(tid, topic, kind="escalation")
            row = self.db.q1("SELECT audience FROM checkpoints WHERE task_id=?",
                             (tid,))
            self.assertEqual(row["audience"], "human", topic)

    def test_a_failing_gate_is_a_sessions_to_make(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "tests_pass", kind="escalation")
        row = self.db.q1("SELECT audience FROM checkpoints WHERE task_id=?", (tid,))
        self.assertEqual(row["audience"], "session")

    def test_an_unknown_topic_is_open_to_either(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "something_new", kind="escalation")
        row = self.db.q1("SELECT audience FROM checkpoints WHERE task_id=?", (tid,))
        self.assertEqual(row["audience"], "any")

    def test_the_split_is_configurable(self):
        self.set_config(**{"session.human_only": ["signoff"]})
        tid = self.add_card(card_type="t")
        self.cp(tid, "signoff")
        row = self.db.q1("SELECT audience FROM checkpoints WHERE task_id=?", (tid,))
        self.assertEqual(row["audience"], "human")


class TestAttend(AttendCase):
    def test_it_returns_a_decision_that_is_the_sessions(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "signoff", question="Sign off on the mul() card")
        code, packet = W.attend(self.db, timeout=2, interval=0.05)
        self.assertEqual(code, W.DECIDE)
        self.assertEqual(packet["checkpoint"]["question"],
                         "Sign off on the mul() card")

    def test_the_packet_carries_enough_to_judge_without_digging(self):
        tid = self.add_card("Add mul()", card_type="t",
                            brief="src/calc.py has add(). Add mul(a, b).",
                            acceptance=["pytest -q passes"])
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,summary,"
                  "started_at) VALUES ('r_1',?,'build','developer','finished',"
                  "'added mul and a test',0)", (tid,))
        self.db.x("INSERT INTO gate_runs (task_id,gate,hook,verdict,reason,ts) "
                  "VALUES (?,'tests_pass','pre_complete','pass','clean',0)", (tid,))
        self.cp(tid, "signoff", bundle={"changed_files": ["src/calc.py"],
                                        "diff": "+def mul(a, b): ..."})
        _, p = W.attend(self.db, timeout=2, interval=0.05)
        self.assertEqual(p["card"]["title"], "Add mul()")
        self.assertIn("Add mul(a, b)", p["card"]["brief"])
        self.assertEqual(p["card"]["acceptance"], ["pytest -q passes"])
        self.assertIn("added mul", p["what_happened"]["summary"])
        self.assertIn("def mul", p["what_happened"]["diff"])
        self.assertTrue(p["what_happened"]["gates"])

    def test_a_human_only_decision_is_relayed_not_answered(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, status=B.CHECKPOINT)
        self.cp(tid, "no_secrets", kind="escalation")
        code, packet = W.attend(self.db, timeout=2, interval=0.05)
        self.assertEqual(code, W.RELAY)
        self.assertEqual(packet["checkpoint"]["audience"], "human")

    def test_work_still_running_is_not_mistaken_for_a_finished_board(self):
        self.add_card(card_type="t")          # queued, therefore active
        code, packet = W.attend(self.db, timeout=0.3, interval=0.05)
        self.assertEqual(code, W.TIMEOUT)
        self.assertTrue(packet["working"])

    def test_an_idle_clear_board_says_so(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, status=B.DONE)
        code, packet = W.attend(self.db, timeout=2, interval=0.05)
        self.assertEqual(code, W.OK)
        self.assertIsNone(packet)

    def test_it_returns_the_moment_a_decision_appears(self):
        import threading
        tid = self.add_card(card_type="t")

        def raise_it():
            time.sleep(0.2)
            self.cp(tid, "signoff")

        threading.Thread(target=raise_it, daemon=True).start()
        started = time.time()
        code, _ = W.attend(self.db, timeout=10, interval=0.05)
        self.assertEqual(code, W.DECIDE)
        self.assertLess(time.time() - started, 5)

    def test_a_human_attending_sees_the_human_only_one(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "no_secrets", kind="escalation")
        code, _packet = W.attend(self.db, timeout=2, interval=0.05,
                                audience="human")
        self.assertEqual(code, W.DECIDE)

    def test_dead_cards_with_nothing_open_are_surfaced(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, status=B.DEADLETTER, block_reason="gave up")
        code, packet = W.attend(self.db, timeout=2, interval=0.05)
        self.assertEqual(code, W.FAILED)
        self.assertEqual(packet["failed"][0]["id"], tid)


class TestAttendCommand(AttendCase):
    def test_the_rendered_packet_says_what_to_run(self):
        tid = self.add_card("Add mul()", card_type="t")
        self.cp(tid, "signoff")
        rc, out = self.run_cli(["--root", self.root, "attend", "--timeout", "2",
                                "--interval", "0.05"])
        self.assertEqual(rc, W.DECIDE)
        self.assertIn("dispatch respond", out)
        self.assertIn("--as session", out)

    def test_a_relay_tells_the_session_to_stop(self):
        tid = self.add_card(card_type="t")
        B.update(self.db, tid, status=B.CHECKPOINT)
        self.cp(tid, "budget_remaining", kind="escalation")
        rc, out = self.run_cli(["--root", self.root, "attend", "--timeout", "2",
                                "--interval", "0.05"])
        self.assertEqual(rc, W.RELAY)
        self.assertIn("only a person may answer", out)
        self.assertNotIn("dispatch respond", out)

    def test_json_is_available_for_a_caller_that_wants_structure(self):
        tid = self.add_card(card_type="t")
        self.cp(tid, "signoff")
        _rc, out = self.run_cli(["--root", self.root, "attend", "--timeout", "2",
                                "--interval", "0.05", "--json"])
        body = json.loads(out[out.index("{"):])
        self.assertEqual(body["exit"], W.DECIDE)
        self.assertIn("card", body)


class TestRespondAsSession(AttendCase):
    def test_a_session_may_answer_what_is_theirs(self):
        tid = self.add_card(card_type="t")
        cid = self.cp(tid, "signoff")
        rc, _ = self.run_cli(["--root", self.root, "respond", cid, "approve",
                              "--as", "session"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.db.q1("SELECT status FROM checkpoints WHERE id=?",
                                    (cid,))["status"], "approved")

    def test_a_session_may_not_answer_what_is_not(self):
        tid = self.add_card(card_type="t")
        cid = self.cp(tid, "no_secrets", kind="escalation")
        rc, out = self.run_cli(["--root", self.root, "respond", cid, "approve",
                                "--as", "session"])
        self.assertEqual(rc, 1)
        self.assertIn("not a session's to answer", out)
        self.assertEqual(self.db.q1("SELECT status FROM checkpoints WHERE id=?",
                                    (cid,))["status"], "open")

    def test_a_person_may_answer_anything(self):
        tid = self.add_card(card_type="t")
        cid = self.cp(tid, "no_secrets", kind="escalation")
        rc, _ = self.run_cli(["--root", self.root, "respond", cid, "approve"])
        self.assertEqual(rc, 0)

    def test_who_decided_is_on_the_event_log(self):
        tid = self.add_card(card_type="t")
        cid = self.cp(tid, "signoff")
        self.run_cli(["--root", self.root, "respond", cid, "approve",
                      "--as", "session"])
        row = self.db.q1("SELECT actor FROM events WHERE kind='checkpoint.resolved'")
        self.assertEqual(row["actor"], "session")


class TestNothingIsClippedThatMattersForJudging(AttendCase):
    """A decision tool that clips the reasoning and keeps the conclusion is
    training the reader to rubber-stamp."""

    LONG_ACCOUNT = (
        "Acceptable, pass to signoff, with one significant concern flagged "
        "for the human to weigh.\n\n" + ("filler analysis. " * 400) +
        "\n\nTHE CONCERN: the retry path drops the idempotency key, so a "
        "duplicate charge is possible under load.")

    def test_the_agents_account_is_never_clipped(self):
        # the verdict survives a clip; the caveat at the end does not
        tid = self.add_card("a card", card_type="t")
        self.cp(tid, "signoff", bundle={"summary": self.LONG_ACCOUNT})
        _rc, out = self.run_cli(["--root", self.root, "attend",
                                "--timeout", "2", "--interval", "0.05"])
        self.assertIn("THE CONCERN", out, "the caveat was clipped away")
        self.assertNotIn("clipped", out)

    def test_evidence_is_never_clipped(self):
        tid = self.add_card("a card", card_type="t")
        self.cp(tid, "tests_pass", kind="escalation",
                bundle={"evidence": self.LONG_ACCOUNT})
        _rc, out = self.run_cli(["--root", self.root, "attend",
                                "--timeout", "2", "--interval", "0.05"])
        self.assertIn("THE CONCERN", out)

    def test_the_brief_is_never_clipped(self):
        tid = self.add_card("a card", card_type="t", brief=self.LONG_ACCOUNT)
        self.cp(tid, "signoff")
        _rc, out = self.run_cli(["--root", self.root, "attend",
                                "--timeout", "2", "--interval", "0.05"])
        self.assertIn("THE CONCERN", out)

    def test_a_huge_diff_is_bounded_but_says_where_the_rest_is(self):
        tid = self.add_card("a card", card_type="t")
        self.cp(tid, "signoff", bundle={"diff": "+line\n" * 4000})
        _rc, out = self.run_cli(["--root", self.root, "attend",
                                "--timeout", "2", "--interval", "0.05"])
        self.assertIn("clipped", out)
        self.assertIn("dispatch attend --full", out)

    def test_full_prints_the_whole_diff(self):
        tid = self.add_card("a card", card_type="t")
        self.cp(tid, "signoff", bundle={"diff": "+line\n" * 4000})
        _rc, out = self.run_cli(["--root", self.root, "attend", "--full",
                                "--timeout", "2", "--interval", "0.05"])
        self.assertNotIn("clipped", out)


class TestClipsNameARemedyThatExists(BoardCase):
    """REGRESSION: attend printed "`--full` for all of it" and had no such
    flag. The reader tried it, got nothing, and assumed they had mistyped."""
    needs_git = False

    def _flags(self, command):
        from dispatch.cli import build_parser
        sub = build_parser()._subparsers._group_actions[0]
        return {o for a in sub.choices[command]._actions for o in a.option_strings}

    def test_clip_refuses_to_guess_a_remedy(self):
        import inspect

        from dispatch.cli import _clip
        params = inspect.signature(_clip).parameters
        self.assertIs(params["remedy"].default, inspect.Parameter.empty,
                      "a default remedy lets a call site inherit a wrong one")

    def test_every_command_that_offers_full_has_it(self):
        for command in ("show", "plan", "attend"):
            self.assertIn("--full", self._flags(command), command)

    def test_a_clipped_plan_names_a_flag_plan_has(self):
        import json as _json
        tid = self.add_card("direction", card_type="research")
        plan = {"summary": "x", "cards": [
            {"ref": "a", "title": "t", "brief": "y" * 3000,
             "acceptance": ["ok"]}]}
        B.update(self.db, tid, plan=_json.dumps(plan))
        _rc, out = self.run_cli(["--root", self.root, "plan", tid])
        if "clipped" in out:
            self.assertIn("--full", out)
            self.assertIn("--full", self._flags("plan"))

    def test_a_clipped_memory_names_a_command_that_exists(self):
        from dispatch import memory as MEM
        MEM.add(self.db, title="Long one", body="z" * 2000)
        _rc, out = self.run_cli(["--root", self.root, "memory"])
        self.assertIn("clipped", out)
        self.assertIn("dispatch memory show", out)

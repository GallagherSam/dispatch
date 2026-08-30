"""What the arbiter does when it cannot answer, and what that costs.

Every test here has a bug behind it. The arbiter was the one gate that cost
money and the one gate that passed whenever anything went wrong.
"""
import json
import os
import sys

from dispatch import arbiter as A
from dispatch import board as B
from dispatch import gates as G
from tests.helpers import BoardCase


def _probe(path, stdout, exit_code=0):
    """A stand-in for the arbiter CLI that prints what we tell it to."""
    with open(path, "w") as f:
        f.write("import sys\n"
                "sys.stdin.read()\n"
                f"sys.stdout.write({stdout!r})\n"
                f"sys.exit({exit_code})\n")
    return [sys.executable, path]


class TestTheExtractor(BoardCase):
    needs_git = False

    # REGRESSION: this counted braces, so one unbalanced brace inside a string
    # ended the object early and the whole reply became unreadable. The
    # arbiter's own prompt asks for evidence describing what is missing, and
    # evidence about code says things like "missing } in the config block".
    def test_an_unbalanced_brace_in_a_string_survives(self):
        got = A._extract_json('{"verdict":"fail","reason":"missing } in config"}')
        self.assertEqual(got, {"verdict": "fail",
                               "reason": "missing } in config"})

    def test_an_unbalanced_open_brace_survives(self):
        got = A._extract_json('{"verdict":"fail","reason":"an open { brace"}')
        self.assertEqual(got["reason"], "an open { brace")

    def test_escaped_json_inside_a_string_survives(self):
        got = A._extract_json('{"v":"fail","evidence":"expected {\\"a\\":1}"}')
        self.assertEqual(got["evidence"], 'expected {"a":1}')

    def test_prose_before_and_after_is_ignored(self):
        got = A._extract_json(
            'The } here is prose.\n{"verdict":"pass"}\nThat is my answer.')
        self.assertEqual(got, {"verdict": "pass"})

    def test_fences_with_and_without_a_language_tag(self):
        for raw in ('```json\n{"verdict":"pass"}\n```',
                    '```\n{"verdict":"pass"}\n```',
                    '```json {"verdict":"pass"}```'):
            self.assertEqual(A._extract_json(raw), {"verdict": "pass"}, raw)

    def test_a_reply_with_no_object_is_still_none(self):
        self.assertIsNone(A._extract_json("I could not decide."))
        self.assertIsNone(A._extract_json('["not","an","object"]'))


class TestItNoLongerPassesOnFailure(BoardCase):
    needs_git = False

    def _judge(self, task_id):
        return G.BUILTINS["arbiter_judges"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(task_id), "diff": "x", "summary": "did it"}, [])

    # REGRESSION: judge_acceptance returned PASS whenever the arbiter could not
    # be reached, so a crash, a timeout or an empty reply walked the card
    # straight through the only gate that costs money. A card could merge
    # having been judged by nothing at all.
    def test_a_crashed_arbiter_does_not_pass_the_card(self):
        self.set_config(**{"arbiter.command": ["definitely-not-a-real-binary"]})
        tid = self.add_card(acceptance=["it works"])
        v = self._judge(tid)
        self.assertNotEqual(v.verdict, G.PASS,
                            "an unreachable arbiter passed the card")
        self.assertEqual(v.verdict, G.DEFER)

    def test_an_empty_reply_does_not_pass_the_card(self):
        cmd = _probe(os.path.join(self.tmp, "silent.py"), "")
        self.set_config(**{"arbiter.command": cmd})
        tid = self.add_card(acceptance=["it works"])
        self.assertNotEqual(self._judge(tid).verdict, G.PASS)

    # An unreadable reply is not transient: the arbiter answered, we could not
    # use it. Retrying the same prompt tends to produce the same prose.
    def test_an_unreadable_reply_escalates_rather_than_deferring(self):
        cmd = _probe(os.path.join(self.tmp, "prose.py"),
                     "I think this looks fine to me overall.")
        self.set_config(**{"arbiter.command": cmd})
        tid = self.add_card(acceptance=["it works"])
        v = self._judge(tid)
        self.assertEqual(v.verdict, G.ESCALATE)

    # ...and neither is a missing arbiter. Deferring forever on a condition
    # that never changes is a stall wearing a retry's clothes.
    def test_no_arbiter_configured_escalates_rather_than_deferring(self):
        self.set_config(**{"arbiter.command": []})
        tid = self.add_card(acceptance=["it works"])
        v = self._judge(tid)
        self.assertEqual(v.verdict, G.ESCALATE)
        self.assertIn("no arbiter", v.reason + (v.evidence or ""))

    def test_a_real_verdict_still_passes_and_fails_normally(self):
        for reply, want in (('{"verdict":"pass","reason":"met"}', G.PASS),
                            ('{"verdict":"fail","reason":"no tests"}', G.FAIL)):
            cmd = _probe(os.path.join(self.tmp, "judge.py"),
                         json.dumps({"result": reply, "total_cost_usd": 0.01}))
            self.set_config(**{"arbiter.command": cmd})
            tid = self.add_card(acceptance=["it works"])
            self.assertEqual(self._judge(tid).verdict, want, reply)

    def test_deferring_is_bounded_and_then_asks_a_human(self):
        self.set_config(**{"arbiter.command": ["definitely-not-a-real-binary"]})
        tid = self.add_card(acceptance=["it works"])
        seen = [self._judge(tid).verdict for _ in range(A._DEFER_LIMIT + 1)]
        self.assertEqual(seen[0], G.DEFER)
        self.assertEqual(seen[-1], G.ESCALATE,
                         f"never stopped deferring: {seen}")

    def test_a_recovered_arbiter_resets_the_retry_budget(self):
        tid = self.add_card(acceptance=["it works"])
        self.set_config(**{"arbiter.command": ["definitely-not-a-real-binary"]})
        self._judge(tid)
        ok = _probe(os.path.join(self.tmp, "ok.py"),
                    json.dumps({"result": '{"verdict":"pass"}',
                                "total_cost_usd": 0.01}))
        self.set_config(**{"arbiter.command": ok})
        self.assertEqual(self._judge(tid).verdict, G.PASS)
        self.set_config(**{"arbiter.command": ["definitely-not-a-real-binary"]})
        self.assertEqual(self._judge(tid).verdict, G.DEFER,
                         "an old outage still counted against the card")


class TestSpendIsReal(BoardCase):
    needs_git = False

    # REGRESSION: `_call` parsed the CLI envelope, took `.result` and threw
    # `total_cost_usd` away. Arbiter money was invisible to the board total,
    # to subtree budgets and to the budget_remaining gate — the header said
    # "$0.00" while the board was spending on every judgment it made.
    def test_the_cost_of_a_judgment_is_recorded(self):
        cmd = _probe(os.path.join(self.tmp, "judge.py"),
                     json.dumps({"result": '{"verdict":"pass"}',
                                 "total_cost_usd": 0.42}))
        self.set_config(**{"arbiter.command": cmd})
        tid = self.add_card(acceptance=["it works"])
        G.BUILTINS["arbiter_judges"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(tid), "diff": "x", "summary": "s"}, [])
        self.assertAlmostEqual(B.spend(self.db)["arbiter_usd"], 0.42)
        self.assertAlmostEqual(B.spend(self.db)["usd"], 0.42)

    def test_runs_still_counts_only_agent_runs(self):
        # inflating "runs" to make the money add up moves the lie, not fixes it
        cmd = _probe(os.path.join(self.tmp, "judge.py"),
                     json.dumps({"result": '{"verdict":"pass"}',
                                 "total_cost_usd": 0.10}))
        self.set_config(**{"arbiter.command": cmd})
        tid = self.add_card(acceptance=["it works"])
        G.BUILTINS["arbiter_judges"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(tid), "diff": "x", "summary": "s"}, [])
        s = B.spend(self.db)
        self.assertEqual(s["runs"], 0)
        self.assertEqual(s["arbiter_calls"], 1)

    def test_a_failed_call_is_recorded_too(self):
        # a call that crashed after the model answered still cost money, and a
        # call that never ran is still worth seeing when asking why nothing moved
        self.set_config(**{"arbiter.command": ["definitely-not-a-real-binary"]})
        tid = self.add_card(acceptance=["it works"])
        G.BUILTINS["arbiter_judges"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(tid), "diff": "x", "summary": "s"}, [])
        row = self.db.q1("SELECT outcome, purpose FROM arbiter_calls")
        self.assertEqual(row["outcome"], A.UNREACHABLE)
        self.assertEqual(row["purpose"], "judge_acceptance")

    def test_the_board_ceiling_sees_arbiter_spend(self):
        self.set_config(**{"containment.total_budget_usd": 1.0})
        tid = self.add_card(acceptance=["ok"])
        self.db.x("INSERT INTO arbiter_calls (id,task_id,purpose,model,outcome,"
                  "usd,duration_s,created_at) VALUES "
                  "('ac_x',?,'judge_acceptance','sonnet','ok',5.0,1.0,1.0)",
                  (tid,))
        v = G.BUILTINS["budget_remaining"](
            {"db": self.db, "cfg": self.cfg, "root": self.root,
             "task": self.task(tid)}, [])
        self.assertEqual(v.verdict, G.ESCALATE,
                         "the ceiling could not see arbiter spend")

    def test_a_subtree_budget_sees_arbiter_spend(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        self.db.x("INSERT INTO arbiter_calls (id,task_id,purpose,model,outcome,"
                  "usd,duration_s,created_at) VALUES "
                  "('ac_y',?,'judge_acceptance','sonnet','ok',3.0,1.0,1.0)",
                  (child,))
        _cap, spent = B.subtree_budget(self.db, self.cfg, parent)
        self.assertAlmostEqual(spent["usd"], 3.0)

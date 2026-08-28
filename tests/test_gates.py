"""Gate evaluation: four verdicts, precedence, and the external contract."""
import os
import stat

from dispatch import gates as G
from tests.helpers import FAILING_TEST, BoardCase


class TestGlobMatching(BoardCase):
    needs_git = False

    def test_star_does_not_cross_a_slash(self):
        # REGRESSION: fnmatch let `src/*` match `src/a/b/c.py`, so a scope was
        # not actually a scope.
        self.assertTrue(G.glob_match("src/calc.py", ["src/*"]))
        self.assertFalse(G.glob_match("src/deep/calc.py", ["src/*"]))

    def test_double_star_does_cross(self):
        self.assertTrue(G.glob_match("src/deep/nested/calc.py", ["src/**"]))
        self.assertTrue(G.glob_match("a/b/c.py", ["**/c.py"]))
        self.assertTrue(G.glob_match("c.py", ["**/c.py"]))

    def test_non_matches(self):
        self.assertFalse(G.glob_match("README.md", ["src/**", "tests/**"]))
        self.assertFalse(G.glob_match(".pytest_cache/v/cache/nodeids",
                                      ["src/**", "tests/**"]))

    def test_question_mark_and_literals(self):
        self.assertTrue(G.glob_match("a1.py", ["a?.py"]))
        self.assertFalse(G.glob_match("a12.py", ["a?.py"]))
        self.assertTrue(G.glob_match("a.b.c", ["a.b.c"]))
        self.assertFalse(G.glob_match("axbxc", ["a.b.c"]))


class TestSpecParsing(BoardCase):
    needs_git = False

    def test_shorthand_and_object_forms(self):
        self.assertEqual(G.parse_spec("tests_pass"),
                         {"gate": "tests_pass", "args": []})
        self.assertEqual(G.parse_spec("quota_above:30"),
                         {"gate": "quota_above", "args": ["30"]})
        self.assertEqual(G.parse_spec("wip_limit:build,4"),
                         {"gate": "wip_limit", "args": ["build", "4"]})
        d = G.parse_spec({"gate": "tests_pass", "hook": "pre_complete"})
        self.assertEqual(d["gate"], "tests_pass")
        self.assertEqual(d["hook"], "pre_complete")
        self.assertEqual(d["args"], [])


class TestVerdictPrecedence(BoardCase):
    def _ctx(self, task, **extra):
        from dispatch.config import paths
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
               "root": self.root, "paths": paths(self.root), "task": task}
        ctx.update(extra)
        return ctx

    def test_most_restrictive_verdict_wins(self):
        self.set_config(**{"global_gates.pre_dispatch": []})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["always_pass", "always_defer",
                                            "always_fail"]}])
        for name, verdict in (("always_pass", "pass"), ("always_defer", "defer"),
                              ("always_fail", "fail")):
            self._script(name, verdict)
        tid = self.add_card(card_type="t")
        v, trail = G.evaluate(self._ctx(self.task(tid)), "pre_dispatch")
        self.assertEqual(v.verdict, G.FAIL)
        self.assertEqual(len(trail), 3)

    def test_escalate_beats_fail(self):
        self.set_config(**{"global_gates.pre_dispatch": []})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["always_fail", "always_escalate"]}])
        self._script("always_fail", "fail")
        self._script("always_escalate", "escalate")
        tid = self.add_card(card_type="t")
        v, _ = G.evaluate(self._ctx(self.task(tid)), "pre_dispatch")
        self.assertEqual(v.verdict, G.ESCALATE)

    def test_defer_carries_the_longest_backoff(self):
        self.set_config(**{"global_gates.pre_dispatch": []})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["slow_defer", "fast_defer"]}])
        self._script("slow_defer", "defer", retry_after_s=900)
        self._script("fast_defer", "defer", retry_after_s=10)
        tid = self.add_card(card_type="t")
        v, _ = G.evaluate(self._ctx(self.task(tid)), "pre_dispatch")
        self.assertEqual(v.verdict, G.DEFER)
        self.assertEqual(v.retry_after_s, 900)

    def test_a_gate_that_raises_fails_closed_without_killing_the_run(self):
        self.set_config(**{"global_gates.pre_dispatch": []})
        self.only_workflow("t", [{"stage": "build", "agent": "developer",
                                  "gates": ["kaboom"]}])
        G.BUILTINS["kaboom"] = lambda ctx, args: 1 / 0
        try:
            tid = self.add_card(card_type="t")
            v, _ = G.evaluate(self._ctx(self.task(tid)), "pre_dispatch")
            self.assertEqual(v.verdict, G.FAIL)
            self.assertIn("ZeroDivisionError", v.reason)
        finally:
            del G.BUILTINS["kaboom"]

    def _script(self, name, verdict, retry_after_s=0):
        from dispatch.config import paths
        p = os.path.join(paths(self.root)["gates"], name + ".sh")
        with open(p, "w") as f:
            f.write("#!/usr/bin/env bash\ncat > /dev/null\n"
                    f'echo \'{{"verdict":"{verdict}","reason":"scripted",'
                    f'"retry_after_s":{retry_after_s}}}\'\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


class TestExternalGateContract(BoardCase):
    def _ctx(self, task, **extra):
        from dispatch.config import paths
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
               "root": self.root, "paths": paths(self.root), "task": task,
               "hook": "pre_dispatch"}
        ctx.update(extra)
        return ctx

    def _install(self, name, body):
        from dispatch.config import paths
        p = os.path.join(paths(self.root)["gates"], name + ".sh")
        with open(p, "w") as f:
            f.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def test_exit_zero_with_no_output_is_a_pass(self):
        # a one-line shell script should be a valid gate
        self._install("quiet", "cat > /dev/null; exit 0")
        tid = self.add_card()
        v = G.run_external(self._ctx(self.task(tid)), "quiet", [])
        self.assertEqual(v.verdict, G.PASS)

    def test_nonzero_exit_is_a_fail_carrying_output_as_evidence(self):
        self._install("noisy", "cat > /dev/null; echo 'the reason' >&2; exit 3")
        tid = self.add_card()
        v = G.run_external(self._ctx(self.task(tid)), "noisy", [])
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("the reason", v.evidence)

    def test_the_card_arrives_on_stdin(self):
        self._install("echoer",
                      "payload=$(cat); "
                      "python3 -c \"import json,sys; d=json.loads(sys.argv[1]); "
                      "print(json.dumps({'verdict':'pass','reason':d['title']}))\" "
                      "\"$payload\"")
        tid = self.add_card("a distinctive title")
        v = G.run_external(self._ctx(self.task(tid)), "echoer", [])
        self.assertEqual(v.verdict, G.PASS)
        self.assertEqual(v.reason, "a distinctive title")

    def test_unknown_gate_fails_rather_than_silently_passing(self):
        tid = self.add_card()
        v = G.run_external(self._ctx(self.task(tid)), "no_such_gate", [])
        self.assertEqual(v.verdict, G.FAIL)


class TestBuiltins(BoardCase):
    def _ctx(self, task, **extra):
        from dispatch.config import paths
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.wfs,
               "root": self.root, "paths": paths(self.root), "task": task}
        ctx.update(extra)
        return ctx

    def test_has_acceptance_escalates_before_dispatch(self):
        # REGRESSION: this used to fail at pre_complete, burning three agent
        # runs to discover a card nobody could check.
        self.assertEqual(G.DEFAULT_HOOK["has_acceptance"], "pre_dispatch")
        tid = self.add_card(acceptance=[])
        v = G.BUILTINS["has_acceptance"](self._ctx(self.task(tid)), [])
        self.assertEqual(v.verdict, G.ESCALATE)

    def test_diff_scope_rejects_a_stray_file(self):
        tid = self.add_card(scope=["src/**"])
        ctx = self._ctx(self.task(tid),
                        changed_files=["src/calc.py", "infra/deploy.tf"])
        v = G.BUILTINS["diff_scope"](ctx, [])
        self.assertEqual(v.verdict, G.FAIL)
        self.assertIn("infra/deploy.tf", v.evidence)

    def test_diff_scope_passes_when_nothing_strays(self):
        tid = self.add_card(scope=["src/**", "tests/**"])
        ctx = self._ctx(self.task(tid),
                        changed_files=["src/calc.py", "tests/test_calc.py"])
        self.assertEqual(G.BUILTINS["diff_scope"](ctx, []).verdict, G.PASS)

    def test_no_scope_declared_is_not_a_failure(self):
        tid = self.add_card()
        ctx = self._ctx(self.task(tid), changed_files=["anything.py"])
        self.assertEqual(G.BUILTINS["diff_scope"](ctx, []).verdict, G.PASS)

    def test_no_secrets_escalates_on_an_added_key(self):
        tid = self.add_card()
        diff = ("+++ b/config.py\n"
                "+AWS_KEY = \"AKIAIOSFODNN7EXAMPLE\"\n")
        v = G.BUILTINS["no_secrets"](self._ctx(self.task(tid), diff=diff), [])
        self.assertEqual(v.verdict, G.ESCALATE)

    def test_no_secrets_ignores_removed_lines(self):
        tid = self.add_card()
        diff = "-AWS_KEY = \"AKIAIOSFODNN7EXAMPLE\"\n"
        v = G.BUILTINS["no_secrets"](self._ctx(self.task(tid), diff=diff), [])
        self.assertEqual(v.verdict, G.PASS)

    def test_quota_gate_passes_when_the_probe_is_untaught(self):
        # an unknown quota must not silently stop the board
        tid = self.add_card()
        v = G.BUILTINS["quota_above"](self._ctx(self.task(tid)), ["15"])
        self.assertEqual(v.verdict, G.PASS)

    def test_quota_gate_defers_when_the_probe_says_low(self):
        from dispatch.config import paths
        p = os.path.join(paths(self.root)["gates"], "quota.sh")
        with open(p, "w") as f:
            f.write("#!/usr/bin/env bash\necho 4\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        tid = self.add_card()
        v = G.BUILTINS["quota_above"](self._ctx(self.task(tid)), ["15"])
        self.assertEqual(v.verdict, G.DEFER)
        self.assertGreater(v.retry_after_s, 0)

    def test_tests_pass_reports_the_failure_as_evidence(self):
        self.write("tests/test_calc.py", FAILING_TEST)
        tid = self.add_card()
        ctx = self._ctx(self.task(tid), cwd=self.root)
        v = G.BUILTINS["tests_pass"](ctx, [])
        self.assertEqual(v.verdict, G.FAIL)
        # the command that ran and what it said, so the next attempt's brief
        # can act on it — not just "tests failed"
        self.assertIn("unittest discover", v.evidence)
        self.assertIn("deliberately broken", v.evidence)

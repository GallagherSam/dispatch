"""Which credentials the agents bill against.

An `ANTHROPIC_API_KEY` in the environment silently outranks the claude.ai
login, so a fleet of agents can burn API credits while you believe it is on
your subscription. That is the failure this guards.
"""
import os
from unittest import mock

from dispatch.config import (
    API_AUTH_VARS,
    agent_environment,
    auth_mode,
    auth_note,
    load_config,
)
from tests.helpers import BoardCase


class TestAuthMode(BoardCase):
    needs_git = False

    def test_the_default_is_the_subscription(self):
        self.assertEqual(auth_mode(load_config(self.root)), "subscription")

    def test_subscription_strips_every_api_auth_variable(self):
        fake = dict.fromkeys(API_AUTH_VARS, "secret")
        fake["PATH"] = "/usr/bin"
        env = agent_environment({"runner": {"auth": "subscription"}}, fake)
        for var in API_AUTH_VARS:
            self.assertNotIn(var, env, f"{var} would have outranked the login")
        self.assertEqual(env["PATH"], "/usr/bin", "unrelated vars must survive")

    def test_api_key_mode_leaves_them_in_place(self):
        fake = {"ANTHROPIC_API_KEY": "secret"}
        env = agent_environment({"runner": {"auth": "api_key"}}, fake)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "secret")

    def test_inherit_passes_the_environment_through(self):
        fake = {"ANTHROPIC_API_KEY": "secret", "ANTHROPIC_BASE_URL": "http://x"}
        env = agent_environment({"runner": {"auth": "inherit"}}, fake)
        self.assertEqual(env, fake)

    def test_a_bedrock_or_vertex_switch_is_also_stripped(self):
        for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env = agent_environment({"runner": {"auth": "subscription"}},
                                    {var: "1"})
            self.assertNotIn(var, env)

    def test_a_missing_auth_setting_defaults_to_subscription(self):
        env = agent_environment({"runner": {}}, {"ANTHROPIC_API_KEY": "s"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class TestAuthNote(BoardCase):
    needs_git = False

    def test_it_says_when_a_key_is_being_removed(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "s"}):
            note = auth_note({"runner": {"auth": "subscription"}})
        self.assertIn("subscription", note)
        self.assertIn("ANTHROPIC_API_KEY", note)
        self.assertIn("will be removed", note)

    def test_a_clean_environment_reads_plainly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(auth_note({"runner": {"auth": "subscription"}}),
                             "claude.ai subscription")

    def test_api_key_mode_says_it_is_not_the_subscription(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "s"}):
            self.assertIn("not your subscription",
                          auth_note({"runner": {"auth": "api_key"}}))

    def test_api_key_mode_with_no_key_is_flagged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("not set", auth_note({"runner": {"auth": "api_key"}}))

    def test_a_secret_value_never_appears_in_the_note(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-TOPSECRET"}):
            for mode in ("subscription", "api_key", "inherit"):
                self.assertNotIn("TOPSECRET",
                                 auth_note({"runner": {"auth": mode}}))


class TestRunnerUsesIt(BoardCase):
    def test_the_agent_process_does_not_receive_the_api_key(self):
        # the fake agent writes the environment it saw into its summary
        probe = os.path.join(self.tmp, "probe_agent.py")
        with open(probe, "w") as f:
            f.write(
                "import json,os,sys\n"
                "sys.stdin.read()\n"
                "res=os.environ.get('DISPATCH_RESULT')\n"
                "seen=[v for v in ('ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL')\n"
                "      if os.environ.get(v)]\n"
                "open(res,'w').write(json.dumps({'summary':'saw:'+','.join(seen)}))\n"
                "print(json.dumps({'result':'ok','total_cost_usd':0.0}))\n")
        import sys as _sys
        self.set_config(**{"runner.command": [_sys.executable, probe]})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        tid = self.add_card(card_type="t")

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret",
                                          "ANTHROPIC_BASE_URL": "http://gateway"}):
            from dispatch import runner as R
            res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["summary"], "saw:",
                         "the agent inherited API credentials it should not have")

    def test_api_key_mode_passes_it_through(self):
        probe = os.path.join(self.tmp, "probe_agent2.py")
        with open(probe, "w") as f:
            f.write(
                "import json,os,sys\n"
                "sys.stdin.read()\n"
                "res=os.environ.get('DISPATCH_RESULT')\n"
                "open(res,'w').write(json.dumps({'summary':'saw:'+\n"
                "  ('yes' if os.environ.get('ANTHROPIC_API_KEY') else 'no')}))\n"
                "print(json.dumps({'result':'ok','total_cost_usd':0.0}))\n")
        import sys as _sys
        self.set_config(**{"runner.command": [_sys.executable, probe],
                           "runner.auth": "api_key"})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        tid = self.add_card(card_type="t")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}):
            from dispatch import runner as R
            res = R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))
        self.assertEqual(res["summary"], "saw:yes")


class TestReporting(BoardCase):
    needs_git = False

    def test_status_says_what_the_agents_bill(self):
        _rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertIn("billing", out)

    def test_init_says_what_the_agents_will_bill(self):
        import shutil
        import tempfile
        other = tempfile.mkdtemp(prefix="dispatch-auth-")
        try:
            _rc, out = self.run_cli(["init", other, "--git-init"])
            self.assertIn("agents bill", out)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_init_can_set_the_mode(self):
        import shutil
        import tempfile
        other = tempfile.mkdtemp(prefix="dispatch-auth2-")
        try:
            self.run_cli(["init", other, "--git-init", "--auth", "api_key"])
            self.assertEqual(load_config(other)["runner"]["auth"], "api_key")
        finally:
            shutil.rmtree(other, ignore_errors=True)

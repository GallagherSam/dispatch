"""OS-level confinement of agent processes.

Two backends with different bargains: the filesystem-only ones leave the
internet open, srt also locks egress down.
"""
import json
import os
import platform
import unittest
from unittest import mock

from dispatch import sandbox as SB
from tests.helpers import BoardCase


def on(**sandbox):
    return {"sandbox": dict(enabled=True, **sandbox)}


class TestBackendSelection(BoardCase):
    needs_git = False

    def test_auto_picks_the_filesystem_only_backend_for_this_os(self):
        got = SB.resolve_backend(on())
        self.assertEqual(got, "seatbelt" if platform.system() == "Darwin"
                         else "bwrap" if platform.system() == "Linux" else "none")

    def test_auto_never_picks_srt(self):
        # the default must not silently break WebFetch and package installs
        with mock.patch.object(platform, "system", return_value="Darwin"):
            self.assertEqual(SB.resolve_backend(on()), "seatbelt")
        with mock.patch.object(platform, "system", return_value="Linux"):
            self.assertEqual(SB.resolve_backend(on()), "bwrap")

    def test_an_explicit_backend_is_honoured(self):
        self.assertEqual(SB.resolve_backend(on(backend="srt")), "srt")

    def test_only_srt_restricts_the_network(self):
        self.assertTrue(SB.restricts_network("srt"))
        for b in ("seatbelt", "bwrap", "none"):
            self.assertFalse(SB.restricts_network(b))

    def test_describe_says_what_happens_to_the_internet(self):
        self.assertIn("internet left open", SB.describe(on()))
        self.assertIn("WebFetch", SB.describe(on(backend="srt")))

    def test_the_shipped_default_confines_where_it_can(self):
        import json as _json

        from dispatch.config import DEFAULT_CONFIG
        cfg = _json.loads(_json.dumps(DEFAULT_CONFIG))
        self.assertEqual(cfg["sandbox"]["enabled"], "auto")
        self.assertEqual(SB.configured_backend(cfg), "auto")


class TestWritePaths(BoardCase):
    needs_git = False

    def test_the_worktree_is_writable(self):
        self.assertIn("/repo/.dispatch/worktrees/t_a",
                      SB.write_paths(on(), "/repo/.dispatch/worktrees/t_a"))

    def test_the_repo_and_the_board_are_not(self):
        # writes are allow-only, so absence is the guarantee
        paths = SB.write_paths(on(), "/repo/.dispatch/worktrees/t_a")
        for denied in ("/repo", "/repo/.dispatch", "/repo/.dispatch/worktrees"):
            self.assertNotIn(denied, paths)

    def test_claude_code_scratch_is_writable(self):
        # without this the agent's Bash tool fails and it works blind
        paths = SB.write_paths(on(), "/wt")
        self.assertTrue(any(f"claude-{os.getuid()}" in p for p in paths), paths)

    def test_extra_paths_are_honoured_and_not_duplicated(self):
        paths = SB.write_paths(on(allow_write=["/opt/tc", "~/.claude"]), "/wt")
        self.assertIn("/opt/tc", paths)
        self.assertEqual(len(paths), len(set(paths)))

    def test_credentials_are_denied_for_reading_by_default(self):
        # resolved, because Seatbelt matches real paths — a rule written
        # against `~` or a symlink never fires
        denied = SB.read_denies(on())
        for secret in ("~/.ssh", "~/.aws", "~/.gnupg"):
            self.assertIn(SB.resolve(secret), denied)
            self.assertNotIn(secret, denied, "an unexpanded ~ never matches")

    def test_null_deny_read_means_the_default_denylist(self):
        self.assertIn(SB.resolve("~/.ssh"), SB.read_denies(on(deny_read=None)))

    def test_an_explicit_empty_deny_read_means_deny_nothing(self):
        # REGRESSION: null and [] were collapsed, so the credential denylist
        # silently became no denylist at all
        self.assertEqual(SB.read_denies(on(deny_read=[])), [])

    def test_the_shipped_config_denies_credential_reads(self):
        from dispatch.config import load_config
        cfg = load_config(self.root)
        cfg["sandbox"]["enabled"] = True
        self.assertIn(SB.resolve("~/.ssh"), SB.read_denies(cfg))


class TestSeatbeltProfile(BoardCase):
    needs_git = False

    def profile(self, **kw):
        return SB.seatbelt_profile(on(**kw), "/repo/.dispatch/worktrees/t_a")

    def test_it_allows_everything_then_narrows_the_filesystem(self):
        p = self.profile()
        self.assertIn("(allow default)", p)
        self.assertIn("(deny file-write*)", p)

    def test_the_network_is_never_mentioned(self):
        # the whole point of this backend: research and installs keep working
        p = self.profile()
        for token in ("network", "network-outbound", "network-inbound"):
            self.assertNotIn(f"(deny {token}", p)

    def test_the_worktree_is_the_first_writable_subpath(self):
        self.assertIn('(subpath "/repo/.dispatch/worktrees/t_a")', self.profile())

    def test_credential_reads_are_denied(self):
        p = self.profile()
        self.assertIn("(deny file-read*", p)
        self.assertIn(os.path.expanduser("~/.ssh"), p)

    def test_an_empty_denylist_emits_no_deny_block(self):
        self.assertNotIn("(deny file-read*", self.profile(deny_read=[]))

    def test_the_cwd_bookkeeping_file_is_writable(self):
        # otherwise every Bash call exits non-zero even when it succeeded
        self.assertIn("-cwd$", self.profile())

    def test_paths_with_quotes_cannot_break_out_of_the_profile(self):
        p = SB.seatbelt_profile(on(allow_write=['/tmp/a"b']), "/wt")
        self.assertIn('\\"', p)


class TestBwrapArgv(BoardCase):
    needs_git = False

    def test_root_is_read_only_and_the_worktree_is_bound_back(self):
        argv = SB.bwrap_argv(on(), self.root)
        self.assertEqual(argv[:5], ["bwrap", "--ro-bind", "/", "/", "--dev"])
        joined = " ".join(argv)
        real = SB.resolve(self.root)
        self.assertIn(f"--bind {real} {real}", joined)

    def test_the_network_namespace_is_left_alone(self):
        self.assertNotIn("--unshare-net", SB.bwrap_argv(on(), self.root))

    def test_it_ends_with_a_separator(self):
        self.assertEqual(SB.bwrap_argv(on(), self.root)[-1], "--")


class TestSrtBackend(BoardCase):
    needs_git = False

    def test_the_model_api_is_reachable(self):
        d = SB.srt_settings(on(backend="srt"), "/wt")["network"]["allowedDomains"]
        self.assertIn("api.anthropic.com", d)

    def test_a_bare_star_is_never_emitted(self):
        # srt rejects it outright, which is why there is no filesystem-only mode
        d = SB.srt_settings(on(backend="srt"), "/wt")["network"]["allowedDomains"]
        self.assertNotIn("*", d)
        self.assertTrue(d, "an empty allowlist means no network at all")

    def test_domains_can_be_replaced_wholesale(self):
        cfg = on(backend="srt", allowed_domains=["api.anthropic.com"])
        self.assertEqual(SB.srt_settings(cfg, "/wt")["network"]["allowedDomains"],
                         ["api.anthropic.com"])

    def test_an_explicit_empty_allowlist_means_no_network(self):
        cfg = on(backend="srt", allowed_domains=[])
        self.assertEqual(SB.srt_settings(cfg, "/wt")["network"]["allowedDomains"], [])

    def test_a_separator_is_always_present(self):
        # REGRESSION: srt and the Claude CLI both take --settings. Without a
        # separator srt swallows the agent's and refuses to run.
        cfg = on(backend="srt",
                 command=["srt", "--settings", "{srt_settings_file}"])
        argv = SB.srt_argv(cfg, "/s.json")
        self.assertEqual(argv[-1], "--")

    def test_the_separator_is_not_doubled(self):
        argv = SB.srt_argv(on(backend="srt"), "/s.json")
        self.assertEqual(argv.count("--"), 1)


class TestWrap(BoardCase):
    needs_git = False

    @unittest.skipUnless(SB.backend_available("seatbelt"),
                         "sandbox-exec is macOS only")
    def test_seatbelt_writes_a_profile_and_prefixes_the_command(self):
        argv, meta = SB.wrap(on(backend="seatbelt"), ["claude", "-p"],
                             self.root, self.tmp)
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertEqual(argv[-2:], ["claude", "-p"])
        self.assertEqual(meta["backend"], "seatbelt")
        self.assertFalse(meta["network_restricted"])
        self.assertTrue(os.path.exists(meta["profile"]))

    def test_srt_writes_settings_and_reports_a_restricted_network(self):
        with mock.patch.object(SB, "backend_available", return_value=True):
            argv, meta = SB.wrap(on(backend="srt"), ["claude", "-p"],
                                 self.root, self.tmp)
        self.assertEqual(argv[0], "srt")
        self.assertTrue(meta["network_restricted"])
        with open(meta["profile"]) as f:
            self.assertIn("allowWrite", json.load(f)["filesystem"])

    def test_an_unavailable_backend_raises_rather_than_running_unconfined(self):
        with mock.patch.object(SB, "backend_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                SB.wrap(on(backend="srt"), ["claude"], self.root, self.tmp)
        self.assertIn("will not quietly", str(ctx.exception))

    def test_an_unsupported_platform_raises(self):
        with mock.patch.object(platform, "system", return_value="Plan9"):
            with self.assertRaises(RuntimeError):
                SB.wrap(on(), ["claude"], self.root, self.tmp)


class TestContainmentWarnings(BoardCase):
    needs_git = False

    def test_a_normal_repo_raises_nothing(self):
        self.assertEqual(SB.containment_warnings(on(), "/Users/x/code/app"), [])

    def test_a_repo_inside_a_writable_region_is_flagged(self):
        # exactly the hole that makes the sandbox worthless without noticing
        w = SB.containment_warnings(on(allow_write=["/Users/x/code"]),
                                    "/Users/x/code/app")
        self.assertTrue(any("contains this repo" in x for x in w), w)

    def test_granting_the_whole_home_directory_is_flagged(self):
        w = SB.containment_warnings(on(allow_write=["~"]), "/Users/x/code/app")
        self.assertTrue(any("too broad" in x for x in w), w)

    def test_nothing_is_flagged_when_the_sandbox_is_off(self):
        self.assertEqual(SB.containment_warnings(
            {"sandbox": {"enabled": False, "allow_write": ["/"]}}, "/a"), [])


class TestPreflight(BoardCase):
    needs_git = False

    def test_off_is_allowed_but_says_what_it_costs(self):
        ok, problems = SB.preflight({"sandbox": {"enabled": False}}, self.root)
        self.assertTrue(ok, "off must never stop the board")
        self.assertTrue(any("absolute path" in p for p in problems), problems)

    def test_a_missing_backend_is_refused_with_an_install_hint(self):
        # `on()` sets enabled=True, which means "required"
        with mock.patch.object(SB, "backend_available", return_value=False):
            ok, problems = SB.preflight(on(backend="srt"), self.root)
        self.assertFalse(ok)
        self.assertTrue(any("npm install" in p for p in problems))

    def test_the_scheduler_will_not_start_with_a_broken_sandbox(self):
        self.set_config(**{"sandbox.enabled": True})
        sched = self.scheduler()
        with mock.patch.object(SB, "backend_available", return_value=False):
            with self.assertRaises(SystemExit):
                sched._check_sandbox(fatal=True)

    def test_the_scheduler_will_not_dispatch_with_a_broken_sandbox(self):
        self.set_config(**{"sandbox.enabled": True})
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        sched = self.scheduler()
        with mock.patch.object(SB, "backend_available", return_value=False):
            for _ in range(3):
                sched.tick()
        self.assertEqual(self.stages_run(tid), [])


#: `wrap` refuses to run unsandboxed when the sandbox is on, so these need a
#: real backend. Skipping honestly is right here — the `confinement` CI job is
#: where a missing backend is a hard error, so this cannot hide a gap.
_HAS_BACKEND = any(SB.backend_available(b) for b in ("seatbelt", "bwrap"))


@unittest.skipUnless(_HAS_BACKEND, "no sandbox backend on this host")
class TestRunnerIntegration(BoardCase):
    def _run(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"}])
        self.plan_agent({"*": {}})
        tid = self.add_card(card_type="t")
        from dispatch import runner as R
        return R.launch(self.db, self.root, self.cfg, self.wfs, self.task(tid))

    def test_the_agent_really_runs_inside_the_sandbox(self):
        self.set_config(**{"sandbox.enabled": True})
        res = self._run()
        self.assertEqual(res["exit_code"], 0, "the sandboxed agent did not run")
        ev = self.db.q1("SELECT data FROM events WHERE kind='run.started'")
        data = json.loads(ev["data"])
        self.assertTrue(data["sandboxed"])
        self.assertEqual(data["sandbox"], SB.resolve_backend(self.cfg))

    def test_the_generated_profile_scopes_writes_to_this_worktree(self):
        self.set_config(**{"sandbox.enabled": True})
        res = self._run()
        profile = os.path.join(res["log_dir"], "sandbox.sb")
        if not os.path.exists(profile):
            self.skipTest("not a seatbelt host")
        with open(profile) as f:
            body = f.read()
        self.assertIn(res["cwd"], body)
        self.assertNotIn(f'(subpath "{self.root}")', body)

    def test_nothing_is_wrapped_when_the_sandbox_is_off(self):
        self.set_config(**{"sandbox.enabled": False})
        self._run()
        data = json.loads(self.db.q1(
            "SELECT data FROM events WHERE kind='run.started'")["data"])
        self.assertFalse(data["sandboxed"])
        self.assertNotIn("sandbox-exec", data["cmd"])


class TestSymlinkedPaths(BoardCase):
    """REGRESSION: Seatbelt matches the real path, so a rule written against a
    symlinked one never fired. On macOS `/tmp` is a symlink, which left an
    agent unable to write to its own worktree — surfacing as an unexplained
    permission error inside the card rather than as anything about the
    sandbox."""
    needs_git = False

    def test_a_symlinked_worktree_becomes_a_real_rule(self):
        if not os.path.islink("/tmp"):
            self.skipTest("/tmp is not a symlink on this host")
        profile = SB.seatbelt_profile(on(), "/tmp/some/worktree")
        self.assertIn(os.path.realpath("/tmp") + "/some/worktree", profile)

    def test_a_tilde_never_reaches_a_rule(self):
        profile = SB.seatbelt_profile(on(allow_write=["~/scratch"]), "/wt")
        self.assertNotIn("~", profile)

    def test_globs_survive_resolution(self):
        # there is nothing to resolve, and realpath would mangle them
        self.assertIn("*", SB.resolve("/private/tmp/claude-*-cwd"))

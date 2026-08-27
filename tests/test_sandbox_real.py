"""Confinement, actually exercised rather than asserted.

Everything in test_sandbox checks the argv and the profile we generate. These
run the sandbox for real and try to escape it, which is the only way to know it
works. Each skips when its backend is not on the host, so the macOS job proves
Seatbelt and the Linux job proves bubblewrap.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from dispatch import sandbox as SB
from tests.helpers import BoardCase


def _have(backend):
    return SB.backend_available(backend)


class RealConfinement(BoardCase):
    needs_git = False
    backend = None

    def setUp(self):
        if self.backend is None:
            self.skipTest("base class")
        super().setUp()
        # NOT under $TMPDIR: that is on the writable list (agents' tooling needs
        # it), so a fixture built there is inside the sandbox and proves
        # nothing. This is the same trap `containment_warnings` exists to catch.
        self.sbx = tempfile.mkdtemp(dir="/tmp", prefix="dispatch-sbx-")
        self.inside = os.path.join(self.sbx, "worktree")
        self.outside = os.path.join(self.sbx, "elsewhere")
        os.makedirs(self.inside, exist_ok=True)
        os.makedirs(self.outside, exist_ok=True)
        self.protected = os.path.join(self.outside, "protected.txt")
        with open(self.protected, "w") as f:
            f.write("must not change\n")

    def tearDown(self):
        shutil.rmtree(self.sbx, ignore_errors=True)
        super().tearDown()

    def test_the_fixture_is_outside_every_writable_region(self):
        """If this fails the others prove nothing."""
        cfg = {"sandbox": {"enabled": True, "backend": self.backend}}
        allowed = [os.path.realpath(os.path.expanduser(p.split("*")[0]))
                   for p in SB.write_paths(cfg, self.inside)
                   if p != self.inside]
        target = os.path.realpath(self.protected)
        for region in allowed:
            self.assertFalse(target == region or target.startswith(region + os.sep),
                             f"the fixture sits inside writable {region}")

    def run_confined(self, script):
        cfg = {"sandbox": {"enabled": True, "backend": self.backend}}
        argv, _meta = SB.wrap(cfg, ["/bin/sh", "-c", script],
                              self.inside, self.tmp)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def test_it_can_write_inside_its_worktree(self):
        target = os.path.join(self.inside, "ok.txt")
        out = self.run_confined(f"echo written > {target}")
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(target) as f:
            self.assertEqual(f.read().strip(), "written")

    def test_it_cannot_write_outside_its_worktree(self):
        out = self.run_confined(f"echo HACKED > {self.protected}")
        self.assertNotEqual(out.returncode, 0,
                            "the sandbox let a write escape the worktree")
        with open(self.protected) as f:
            self.assertEqual(f.read().strip(), "must not change")

    def test_it_cannot_delete_outside_its_worktree(self):
        self.run_confined(f"rm -f {self.protected}")
        self.assertTrue(os.path.exists(self.protected),
                        "the sandbox let a delete escape the worktree")


@unittest.skipUnless(_have("seatbelt"), "sandbox-exec is macOS only")
class TestSeatbeltReallyConfines(RealConfinement):
    backend = "seatbelt"

    def test_the_network_is_deliberately_untouched(self):
        # the filesystem-only backend exists so agents can still research
        out = self.run_confined(
            "command -v curl >/dev/null && "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20 "
            "https://example.com || echo skipped")
        self.assertNotIn("000", out.stdout,
                         "the filesystem-only backend blocked the network")

    def test_credential_directories_cannot_be_read(self):
        ssh = os.path.expanduser("~/.ssh")
        if not os.path.isdir(ssh):
            self.skipTest("no ~/.ssh on this host")
        out = self.run_confined(f"ls {ssh}")
        self.assertNotEqual(out.returncode, 0, "the sandbox exposed ~/.ssh")


@unittest.skipUnless(_have("bwrap"), "bubblewrap is not installed")
class TestBwrapReallyConfines(RealConfinement):
    backend = "bwrap"

    def test_the_network_is_deliberately_untouched(self):
        out = self.run_confined(
            "command -v curl >/dev/null && "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20 "
            "https://example.com || echo skipped")
        self.assertNotIn("000", out.stdout,
                         "the filesystem-only backend blocked the network")

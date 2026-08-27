"""Where the board listens: a stable port per repo, and tailnet binding."""
from unittest import mock

from dispatch import net as N
from tests.helpers import BoardCase


class TestPorts(BoardCase):
    needs_git = False

    def test_a_repo_always_gets_the_same_port(self):
        self.assertEqual(N.auto_port(self.root), N.auto_port(self.root))

    def test_different_repos_usually_get_different_ports(self):
        ports = {N.auto_port(f"/tmp/repo-{i}") for i in range(40)}
        self.assertGreater(len(ports), 25, "auto ports collide far too often")

    def test_auto_ports_stay_in_the_documented_range(self):
        for i in range(200):
            p = N.auto_port(f"/x/{i}")
            self.assertGreaterEqual(p, N.AUTO_PORT_BASE)
            self.assertLess(p, N.AUTO_PORT_BASE + N.AUTO_PORT_SPAN)

    def test_an_explicit_port_wins_over_everything(self):
        cfg = {"server": {"port": 9000}}
        self.assertEqual(N.resolve_port(cfg, self.root, 1234), 1234)

    def test_a_configured_port_is_used_as_given(self):
        self.assertEqual(N.resolve_port({"server": {"port": 9000}}, self.root), 9000)

    def test_auto_means_derive_it(self):
        cfg = {"server": {"port": "auto"}}
        self.assertEqual(N.resolve_port(cfg, self.root), N.auto_port(self.root))

    def test_nonsense_falls_back_to_auto_rather_than_crashing(self):
        cfg = {"server": {"port": "banana"}}
        self.assertEqual(N.resolve_port(cfg, self.root), N.auto_port(self.root))

    def test_the_default_config_uses_auto(self):
        from dispatch.config import load_config
        self.assertEqual(load_config(self.root)["server"]["port"], "auto")


class TestHosts(BoardCase):
    needs_git = False

    def test_local_is_loopback_and_silent(self):
        host, warnings = N.resolve_host({"server": {"host": "local"}})
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(warnings, [])

    def test_an_override_beats_the_config(self):
        host, _ = N.resolve_host({"server": {"host": "any"}}, "local")
        self.assertEqual(host, "127.0.0.1")

    def test_tailscale_binds_to_the_tailnet_address(self):
        with mock.patch.object(N, "tailscale_ip", return_value="100.64.1.2"):
            host, warnings = N.resolve_host({}, "tailscale")
        self.assertEqual(host, "100.64.1.2")
        self.assertTrue(any("tailnet" in w for w in warnings))

    def test_tailscale_falls_back_to_loopback_when_it_is_not_up(self):
        # binding to every interface by accident would be much worse
        with mock.patch.object(N, "tailscale_ip", return_value=None):
            host, warnings = N.resolve_host({}, "tailscale")
        self.assertEqual(host, "127.0.0.1")
        self.assertTrue(any("falling back" in w for w in warnings))

    def test_any_warns_that_there_is_no_login(self):
        host, warnings = N.resolve_host({}, "any")
        self.assertEqual(host, "0.0.0.0")
        self.assertTrue(any("no login" in w for w in warnings))

    def test_an_explicit_non_loopback_address_warns(self):
        host, warnings = N.resolve_host({}, "192.168.1.50")
        self.assertEqual(host, "192.168.1.50")
        self.assertTrue(warnings)

    def test_urls_prefer_the_magicdns_name(self):
        with mock.patch.object(N, "tailscale_ip", return_value="100.64.1.2"), \
             mock.patch.object(N, "tailscale_name", return_value="box.tail1.ts.net"):
            urls = N.display_urls("100.64.1.2", 7788)
        self.assertEqual(urls[0], "http://box.tail1.ts.net:7788")
        self.assertIn("http://100.64.1.2:7788", urls)

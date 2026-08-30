"""The built-in manual and the command that reads it."""
import os
import re

from dispatch import help as H
from tests.helpers import BoardCase


class TestManualContent(BoardCase):
    needs_git = False

    def test_every_topic_has_a_title_and_a_summary(self):
        self.assertTrue(H.available())
        for topic in H.available():
            title, note = H.summary(topic)
            self.assertNotEqual(title, topic, f"{topic} has no '# Title'")
            self.assertTrue(note, f"{topic} has no '> summary' line")

    def test_the_reading_order_covers_every_topic(self):
        on_disk = {f[:-3] for f in os.listdir(H.DOCS_DIR) if f.endswith(".md")}
        self.assertEqual(on_disk - set(H.ORDER), set(),
                         "a topic exists that the reading order does not place")

    #: `cli` and `config` are reference tables that grow with the surface they
    #: describe; prose topics have no such excuse.
    REFERENCE = {"cli", "config"}

    def test_topics_stay_concise(self):
        for topic in H.available():
            lines = len((H.read(topic) or "").splitlines())
            cap = 200 if topic in self.REFERENCE else 130
            self.assertLess(lines, cap, f"{topic} is drifting long ({lines})")

    def test_every_command_the_manual_shows_exists(self):
        """Only look at real invocations — in backticks, or in a code block —
        so prose like "dispatch pauses dispatch" is not mistaken for a command."""
        from dispatch.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        shown = set()
        for topic in H.available():
            body = H.read(topic) or ""
            shown |= set(re.findall(r"`dispatch ([a-z]+)", body))
            shown |= set(re.findall(r"^\s{2,}dispatch ([a-z]+)", body, re.M))
        self.assertTrue(shown, "the manual shows no commands at all")
        self.assertEqual(shown - real, set(),
                         "the manual shows commands that do not exist")

    def test_every_command_appears_somewhere_in_the_manual(self):
        from dispatch.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        body = H.whole_manual()
        missing = {c for c in real if f"dispatch {c}" not in body}
        self.assertEqual(missing, set(), "commands the manual never mentions")

    def test_the_config_topic_names_real_settings(self):
        from dispatch.config import DEFAULT_CONFIG
        body = H.read("config") or ""
        for key in ("max_concurrent", "expansion_ratio_limit", "merge_on_done",
                    "permission_mode", "autonomy", "global_gates"):
            self.assertIn(key, body, f"config docs omit {key}")
        for key in DEFAULT_CONFIG["runner"]:
            self.assertIn(key, body, f"config docs omit runner.{key}")

    def test_the_gates_topic_names_every_builtin(self):
        from dispatch.gates import BUILTINS
        body = H.read("gates") or ""
        for name in BUILTINS:
            self.assertIn(name, body, f"gates docs omit {name}")

    def test_the_proposals_topic_names_every_kind(self):
        from dispatch.proposals import KINDS
        body = H.read("proposals") or ""
        for kind in KINDS:
            self.assertIn(kind, body, f"proposals docs omit {kind}")


class TestLookup(BoardCase):
    needs_git = False

    def test_exact_and_prefix_resolution(self):
        self.assertEqual(H.resolve("gates"), "gates")
        self.assertEqual(H.resolve("check"), "checkpoints")
        self.assertEqual(H.resolve("trouble"), "troubleshooting")

    def test_an_unknown_topic_resolves_to_nothing(self):
        self.assertIsNone(H.resolve("nonsense"))

    def test_pagination_covers_the_whole_topic(self):
        body = H.read("cli") or ""
        seen = []
        page, total = 1, 2
        while page <= total:
            chunk, _page_no, total = H.paginate(body, page, 20)
            seen.append(chunk)
            page += 1
        self.assertEqual("\n".join(seen).strip(), body.strip())

    def test_pagination_clamps_out_of_range_pages(self):
        _, page, total = H.paginate(H.read("cli") or "", 999, 20)
        self.assertEqual(page, total)

    def test_search_finds_a_term_and_says_where(self):
        hits = H.search("expansion")
        self.assertTrue(hits)
        self.assertIn("proposals", {t for t, _, _ in hits})


class TestDocsCommand(BoardCase):
    needs_git = False

    def test_the_index_lists_topics_with_summaries(self):
        rc, out = self.run_cli(["docs"])
        self.assertEqual(rc, 0)
        for topic in ("overview", "setup", "cards", "gates"):
            self.assertIn(topic, out)
        self.assertIn("Four verdicts", out)

    def test_reading_a_topic(self):
        rc, out = self.run_cli(["docs", "gates"])
        self.assertEqual(rc, 0)
        self.assertIn("escalate", out)

    def test_a_prefix_is_enough(self):
        rc, out = self.run_cli(["docs", "check"])
        self.assertEqual(rc, 0)
        self.assertIn("Checkpoints", out)

    def test_paging_shows_where_you_are(self):
        rc, out = self.run_cli(["docs", "cli", "--page", "1", "--lines", "15"])
        self.assertEqual(rc, 0)
        self.assertIn("page 1/", out)
        self.assertIn("--page 2", out)

    def test_the_last_page_says_so(self):
        _rc, out = self.run_cli(["docs", "cli", "--page", "99", "--lines", "15"])
        self.assertIn("(end)", out)

    def test_search_from_the_command_line(self):
        rc, out = self.run_cli(["docs", "--search", "worktree"])
        self.assertEqual(rc, 0)
        self.assertIn("dispatch docs", out)

    def test_search_with_no_hits_is_a_clean_miss(self):
        rc, out = self.run_cli(["docs", "--search", "zzzznotathing"])
        self.assertEqual(rc, 1)
        self.assertIn("nothing in the manual", out)

    def test_all_prints_every_topic(self):
        rc, out = self.run_cli(["docs", "--all"])
        self.assertEqual(rc, 0)
        for topic in H.available():
            self.assertIn(f"dispatch docs {topic}", out)

    def test_an_unknown_topic_lists_the_real_ones(self):
        rc, out = self.run_cli(["docs", "nonsense"])
        self.assertEqual(rc, 1)
        self.assertIn("no such topic", out)
        self.assertIn("overview", out)

    def test_docs_work_with_no_board_at_all(self):
        import tempfile
        empty = tempfile.mkdtemp(prefix="dispatch-nodocs-")
        rc, out = self.run_cli(["--root", empty, "docs", "overview"])
        self.assertEqual(rc, 0)
        self.assertIn("Overview", out)


class TestDiscoverability(BoardCase):
    def test_init_points_at_the_manual(self):
        import shutil
        import tempfile
        other = tempfile.mkdtemp(prefix="dispatch-init-")
        try:
            _rc, out = self.run_cli(["init", other, "--git-init"])
            self.assertIn("dispatch docs setup", out)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_agent_prompts_point_at_the_manual(self):
        from dispatch.config import paths
        d = paths(self.root)["agents"]
        for fn in os.listdir(d):
            with open(os.path.join(d, fn)) as f:
                self.assertIn("dispatch docs", f.read(), f"{fn} does not")


class TestExportedDocsStayInSync(BoardCase):
    """`docs/` is generated from the packaged manual. Two copies that can drift
    is worse than one copy, so this fails if they have."""
    needs_git = False

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _exported(self):
        return os.path.join(self.REPO, "docs")

    def test_every_topic_has_an_exported_copy(self):
        missing = [t for t in H.available()
                   if not os.path.exists(os.path.join(self._exported(), t + ".md"))]
        self.assertEqual(missing, [],
                         "run `dispatch docs --export docs/`")

    def test_the_exported_copies_match_the_manual(self):
        drifted = []
        for topic in H.available():
            p = os.path.join(self._exported(), topic + ".md")
            if not os.path.exists(p):
                continue
            with open(p) as f:
                on_disk = f.read().rstrip("\n")
            if on_disk != (H.read(topic) or "").rstrip("\n"):
                drifted.append(topic)
        self.assertEqual(drifted, [],
                         "docs/ is stale — run `dispatch docs --export docs/`")

    def test_nothing_extra_is_left_behind(self):
        known = set(H.available()) | {"README"}
        stale = [f[:-3] for f in os.listdir(self._exported())
                 if f.endswith(".md") and f[:-3] not in known]
        self.assertEqual(stale, [], "topics removed from the manual but not docs/")

    def test_the_index_links_every_topic(self):
        with open(os.path.join(self._exported(), "README.md")) as f:
            index = f.read()
        for topic in H.available():
            self.assertIn(f"({topic}.md)", index, topic)

    def test_the_readme_points_at_the_docs_it_claims(self):
        with open(os.path.join(self.REPO, "README.md")) as f:
            readme = f.read()
        import re
        for link in set(re.findall(r"\(docs/([a-z]+)\.md\)", readme)):
            self.assertTrue(
                os.path.exists(os.path.join(self._exported(), link + ".md")),
                f"README links docs/{link}.md, which does not exist")

    def test_the_readme_stays_short(self):
        # it grew to duplicate most of the manual once already
        with open(os.path.join(self.REPO, "README.md")) as f:
            lines = len(f.read().splitlines())
        self.assertLess(lines, 160, f"README is drifting long ({lines})")


class TestTheVersionIsStatedOnce(BoardCase):
    needs_git = False

    # The release workflow refuses to build when the tag and pyproject
    # disagree, which is the right place to catch it and a slow one — you find
    # out after tagging. `channel.py` carried a third hardcoded copy that
    # nothing checked at all, so a channel could announce a version the CLI
    # had not been for two releases.
    def test_pyproject_and_the_package_agree(self):
        import pathlib
        import sys

        from dispatch import __version__
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            self.skipTest("tomllib is 3.11+; the release job runs 3.12")
        root = pathlib.Path(__file__).parent.parent
        with open(root / "pyproject.toml", "rb") as f:
            declared = tomllib.load(f)["project"]["version"]
        self.assertEqual(declared, __version__,
                         "pyproject and dispatch.__version__ disagree — the "
                         "release build will refuse the tag")

    def test_nothing_else_hardcodes_a_version(self):
        import pathlib
        import re

        pkg = pathlib.Path(__file__).parent.parent / "dispatch"
        offenders = []
        for py in pkg.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            for n, line in enumerate(py.read_text().splitlines(), 1):
                if re.search(r'"\d+\.\d+\.\d+"', line) and "version" in line.lower():
                    offenders.append(f"{py.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "import __version__ instead of restating it")

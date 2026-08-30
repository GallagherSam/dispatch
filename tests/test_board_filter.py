"""The board filter and what the spend figure admits it does not know."""
import pathlib
import time

from dispatch import board as B
from tests.helpers import BoardCase

WEB = pathlib.Path(__file__).parent.parent / "dispatch/web"


class TestTheFilterExists(BoardCase):
    """These check the filter is *wired*, not that it filters.

    The behaviour is client-side JavaScript and this suite has no browser, so
    a revert that leaves the controls in place and stops applying them passes
    here — measured, not assumed: deleting the filter call breaks none of
    these. What actually verifies the behaviour is the headless-Chrome pass in
    the scratchpad harness, which drives the checkboxes and counts the cards
    that remain. Do not read a green run here as "filtering works".
    """

    needs_git = False

    def test_the_controls_are_on_the_board_pane(self):
        html = (WEB / "index.html").read_text()
        for el in ('id="fDone"', 'id="fCancelled"', 'id="fText"', 'id="fNote"'):
            self.assertIn(el, html, el)

    def test_filtering_is_wired_and_persisted(self):
        js = (WEB / "app.js").read_text()
        self.assertIn("wireFilters()", js)
        self.assertIn("dispatch.filters", js, "the choice is not remembered")
        self.assertIn("localStorage", js)

    def test_storage_access_is_guarded(self):
        # a private window, or a browser blocking site data, throws on access
        js = (WEB / "app.js").read_text()
        block = js[js.index("const FILT = {"):js.index("function parseTags")]
        self.assertEqual(block.count("try {"), 2,
                         "localStorage is touched without a try/catch")

    def test_hiding_is_never_silent(self):
        # a filtered board that looks unfiltered is how you conclude a card
        # was never created
        js = (WEB / "app.js").read_text()
        self.assertIn("' hidden'", js, "nothing reports the hidden count")
        self.assertIn("#fNote", js, "the count is computed but never shown")
        self.assertIn("' hidden'", js,
                      "an emptied column still just says 'empty'")

    def test_the_column_count_shows_both_numbers(self):
        js = (WEB / "app.js").read_text()
        self.assertIn('class="of"', js, "no shown/total in the column head")

    def test_the_style_tokens_used_by_the_filter_exist(self):
        css = (WEB / "style.css").read_text()
        for token in ("--info-edge", "--info", "--rule", "--surface"):
            self.assertIn(token + ":", css, f"{token} is used but never defined")


class TestSpendAdmitsWhatIsStillRunning(BoardCase):
    needs_git = False

    def _run(self, status, usd=None, age=0, task=None):
        """A run, with the lease that proves an agent is really holding it.

        A `running` row on its own is not evidence — see
        TestOrphanedRunsAreNotInFlight for why.
        """
        rid = "r_" + status + str(age)
        tid = task or self.tid
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,attempt,status,"
                  "started_at,usd) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, tid, "build", "developer", 1, status,
                   time.time() - age, usd))
        if status == "running":
            self.db.x("INSERT OR REPLACE INTO leases (task_id,run_id,pid,stage,"
                      "heartbeat_at,expires_at) VALUES (?,?,?,?,?,?)",
                      (tid, rid, 1, "build", time.time(), time.time() + 900))

    def setUp(self):
        super().setUp()
        self.tid = self.add_card("x")

    # A run's cost does not exist until it ends — the agent CLI reports
    # total_cost_usd once, on its final event. So a board with agents working
    # always shows a figure that is behind, and saying by how much is the
    # honest version of "live spend".
    def test_a_running_run_is_counted_but_not_costed(self):
        self._run("finished", 2.50, 600)
        self._run("running", None, 240)
        s = B.spend(self.db)
        self.assertEqual(s["usd"], 2.5)
        self.assertEqual(s["in_flight"], 1)
        self.assertLess(abs(time.time() - s["in_flight_since"] - 240), 5)

    def test_nothing_running_reports_nothing_pending(self):
        self._run("finished", 1.0, 100)
        s = B.spend(self.db)
        self.assertEqual(s["in_flight"], 0)
        self.assertIsNone(s["in_flight_since"])

    def test_the_oldest_unfinished_run_is_the_one_reported(self):
        # a lease is per card, so two live runs means two cards
        self._run("running", None, 100)
        self._run("running", None, 900, task=self.add_card("second"))
        s = B.spend(self.db)
        self.assertEqual(s["in_flight"], 2)
        self.assertLess(abs(time.time() - s["in_flight_since"] - 900), 5)

    def test_a_subtree_only_counts_its_own(self):
        other = self.add_card("other")
        self._run("running", None, 50)
        self._run("running", None, 10, task=other)
        self.assertEqual(B.spend(self.db, [self.tid])["in_flight"], 1)
        self.assertEqual(B.spend(self.db, [])["in_flight"], 0)

    def test_status_says_what_is_not_yet_costed(self):
        self._run("finished", 4.20, 900)
        self._run("running", None, 320)
        rc, out = self.run_cli(["--root", self.root, "status"])
        self.assertEqual(rc, 0)
        self.assertIn("still running", out)
        self.assertIn("not yet costed", out)

    def test_the_api_carries_it(self):
        self._run("running", None, 60)
        from dispatch.server import snapshot
        stats = snapshot(self.root, self.db)["stats"]
        self.assertEqual(stats["in_flight_runs"], 1)
        self.assertIsNotNone(stats["in_flight_since"])

    def test_the_header_shows_it(self):
        js = (WEB / "app.js").read_text()
        self.assertIn("in_flight_runs", js)
        self.assertIn("running</span>", js.replace("\n", "").replace(" ", ""),
                      "the count is fetched but never rendered")


class TestOrphanedRunsAreNotInFlight(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.tid = self.add_card("x")

    def _run(self, rid, age, leased=None):
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,attempt,status,"
                  "started_at) VALUES (?,?,'build','developer',1,'running',?)",
                  (rid, self.tid, time.time() - age))
        if leased is not None:
            self.db.x("INSERT OR REPLACE INTO leases (task_id,run_id,pid,stage,"
                      "heartbeat_at,expires_at) VALUES (?,?,?,?,?,?)",
                      (self.tid, rid, 1, "build", time.time(),
                       time.time() + leased))

    # REGRESSION: `status='running'` is not evidence an agent is working. A run
    # whose process dies without reaching the finish path keeps that status
    # forever — a real board had eleven, the oldest 86 hours old. Counting
    # those as in flight makes the header read "+11 still running" on an idle
    # board, which is both alarming and false.
    def test_a_run_with_no_lease_is_not_in_flight(self):
        self._run("r_orphan", age=86 * 3600)
        self.assertEqual(B.spend(self.db)["in_flight"], 0)
        self.assertIsNone(B.spend(self.db)["in_flight_since"])

    def test_a_run_with_an_expired_lease_is_not_in_flight(self):
        self._run("r_stale", age=7200, leased=-60)
        self.assertEqual(B.spend(self.db)["in_flight"], 0)

    def test_a_genuinely_live_run_still_counts(self):
        self._run("r_live", age=120, leased=900)
        s = B.spend(self.db)
        self.assertEqual(s["in_flight"], 1)
        self.assertLess(abs(time.time() - s["in_flight_since"] - 120), 5)

    def test_orphans_do_not_drag_the_age_backwards(self):
        # the oldest *live* run is the useful number; an orphan from Tuesday
        # would otherwise own it
        self._run("r_old_orphan", age=86 * 3600)
        self._run("r_live2", age=90, leased=900)
        s = B.spend(self.db)
        self.assertEqual(s["in_flight"], 1)
        self.assertLess(abs(time.time() - s["in_flight_since"] - 90), 5)

    def test_a_finished_run_is_never_in_flight(self):
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,attempt,status,"
                  "started_at,usd) VALUES ('r_done',?,'build','developer',1,"
                  "'finished',?,1.0)", (self.tid, time.time() - 300))
        self.assertEqual(B.spend(self.db)["in_flight"], 0)
        self.assertEqual(B.spend(self.db)["usd"], 1.0)

"""The web board's API, over a real socket."""
import json
import urllib.error
import urllib.request

from dispatch import board as B
from dispatch.server import serve
from tests.helpers import BoardCase


class ServerCase(BoardCase):
    needs_git = False

    def setUp(self):
        super().setUp()
        self.httpd = serve(self.root, self.db, "127.0.0.1", 0, block=False)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def send(self, method, path, body=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class TestSnapshot(ServerCase):
    def test_state_carries_everything_the_board_renders(self):
        self.add_card("a card")
        code, state = self.get("/api/state")
        self.assertEqual(code, 200)
        for key in ("tasks", "stages", "workflows", "agents", "checkpoints",
                    "proposals", "scheduler", "stats", "validation", "edges"):
            self.assertIn(key, state)

    def test_each_card_carries_its_blockers_and_dependencies(self):
        a = self.add_card("upstream")
        b = self.add_card("downstream")
        B.link(self.db, a, b)
        _, state = self.get("/api/state")
        card = next(t for t in state["tasks"] if t["id"] == b)
        self.assertEqual(card["deps"], [a])
        self.assertTrue(card["blockers"])
        self.assertEqual(card["dep_titles"][0]["title"], "upstream")

    def test_validation_problems_surface_in_the_snapshot(self):
        self.only_workflow("broken", [{"stage": "build", "agent": "nobody"}])
        _, state = self.get("/api/state")
        self.assertTrue(any("nobody" in p for p in state["validation"]))

    def test_spend_is_reported(self):
        tid = self.add_card()
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,usd,"
                  "started_at) VALUES ('r_1',?,'build','developer','finished',"
                  "1.25,0)", (tid,))
        _, state = self.get("/api/state")
        self.assertEqual(state["stats"]["usd"], 1.25)


class TestCardEndpoints(ServerCase):
    def test_creating_a_card(self):
        code, body = self.send("POST", "/api/task",
                               {"title": "from the board", "card_type": "chore",
                                "acceptance": ["it works"], "scope": ["src/**"],
                                "start": True})
        self.assertEqual(code, 200)
        t = self.task(body["id"])
        self.assertEqual(t["title"], "from the board")
        self.assertEqual(t["workspace"]["scope"], ["src/**"])
        self.assertNotEqual(t["stage"], "backlog")

    def test_reading_one_card_includes_its_history(self):
        tid = self.add_card()
        self.db.x("INSERT INTO runs (id,task_id,stage,agent_type,status,"
                  "started_at) VALUES ('r_1',?,'build','developer','finished',0)",
                  (tid,))
        code, body = self.get("/api/task/" + tid)
        self.assertEqual(code, 200)
        self.assertEqual(len(body["runs"]), 1)
        self.assertIn("gate_runs", body)
        self.assertIn("events", body)

    def test_a_missing_card_is_a_404(self):
        code, _ = self.send("GET", "/api/task/t_nope00")
        self.assertEqual(code, 404)

    def test_dragging_a_card_moves_it_and_reassigns_the_agent(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        code, _ = self.send("POST", f"/api/task/{tid}/move", {"stage": "review"})
        self.assertEqual(code, 200)
        t = self.task(tid)
        self.assertEqual(t["stage"], "review")
        self.assertEqual(t["agent_type"], "reviewer")
        self.assertTrue(self.db.q1("SELECT id FROM events WHERE kind='task.moved'"))

    def test_editing_a_card(self):
        tid = self.add_card()
        code, _ = self.send("PUT", "/api/task/" + tid,
                            {"brief": "rewritten", "acceptance": ["a", "b"],
                             "scope": ["lib/**"]})
        self.assertEqual(code, 200)
        t = self.task(tid)
        self.assertEqual(t["brief"], "rewritten")
        self.assertEqual(t["acceptance"], ["a", "b"])
        self.assertEqual(t["workspace"]["scope"], ["lib/**"])

    def test_an_edge_that_would_cycle_is_a_400(self):
        a, b = self.add_card("a"), self.add_card("b")
        self.send("POST", f"/api/task/{b}/edge", {"src": a})
        code, body = self.send("POST", f"/api/task/{a}/edge", {"src": b})
        self.assertEqual(code, 400)
        self.assertIn("cycle", body["error"])

    def test_cancelling_from_the_board(self):
        parent = self.add_card("parent")
        child = self.add_card("child", parent_id=parent)
        self.send("POST", f"/api/task/{parent}/cancel")
        self.assertEqual(self.task(child)["status"], B.CANCELLED)


class TestCheckpointEndpoint(ServerCase):
    def test_responding_to_a_checkpoint(self):
        self.only_workflow("t", [{"stage": "build", "agent": "developer"},
                                 {"stage": "review", "agent": "reviewer"}])
        tid = self.add_card(card_type="t")
        cid = B.open_checkpoint(self.db, tid, "sign off?")
        code, _ = self.send("POST", f"/api/checkpoint/{cid}/respond",
                            {"response": "approve"})
        self.assertEqual(code, 200)
        self.assertEqual(self.task(tid)["stage"], "review")


class TestWorkflowEndpoints(ServerCase):
    def test_saving_a_pipeline_returns_its_problems(self):
        code, body = self.send("PUT", "/api/workflows", {"card_types": {
            "t": {"label": "T", "stages": [{"stage": "build", "agent": "nobody"}]}}})
        self.assertEqual(code, 200)
        self.assertTrue(any("nobody" in p for p in body["problems"]))

    def test_saving_writes_the_exportable_file(self):
        from dispatch.config import paths
        self.send("PUT", "/api/workflows", {"card_types": {
            "t": {"label": "T", "stages": [{"stage": "build",
                                            "agent": "developer"}]}}})
        with open(paths(self.root)["workflows"]) as f:
            self.assertIn("t", json.load(f)["card_types"])

    def test_importing_replaces_the_set(self):
        code, _ = self.send("POST", "/api/workflows/import", {"card_types": {
            "only": {"label": "Only", "stages": [{"stage": "build",
                                                  "agent": "developer"}]}}})
        self.assertEqual(code, 200)
        _, state = self.get("/api/state")
        self.assertEqual(sorted(state["workflows"]), ["only"])


class TestDiagnostics(ServerCase):
    def test_blocked_endpoint_explains_each_hold(self):
        a, b = self.add_card("upstream"), self.add_card("downstream")
        B.link(self.db, a, b)
        code, body = self.get("/api/blocked")
        self.assertEqual(code, 200)
        entry = next(x for x in body["blocked"] if x["id"] == b)
        self.assertTrue(any("waits on" in s for s in entry["blockers"]))

    def test_pausing_the_scheduler(self):
        from dispatch.config import load_config
        code, _ = self.send("POST", "/api/scheduler", {"paused": True})
        self.assertEqual(code, 200)
        self.assertTrue(load_config(self.root)["scheduler"]["paused"])

    def test_the_page_and_its_assets_are_served(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(len(r.read()) > 100, path)




class TestBoundaries(ServerCase):
    def test_a_traversal_outside_the_web_directory_is_refused(self):
        import http.client
        for path in ("/static/../../../../etc/passwd",
                     "/static/..%2f..%2f..%2fetc%2fpasswd",
                     "/static//etc/passwd"):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.httpd.server_address[1], timeout=10)
            conn.putrequest("GET", path, skip_accept_encoding=True)
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)
            self.assertNotIn(b"root:", body, path)

    def test_a_legitimate_asset_still_loads(self):
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_a_cross_origin_write_is_refused(self):
        # the board listens on localhost, which any page in the browser can reach
        req = urllib.request.Request(
            self.base + "/api/task", data=json.dumps({"title": "evil"}).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Origin": "https://evil.example"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 403)
        self.assertIsNone(self.db.q1("SELECT id FROM tasks WHERE title='evil'"))

    def test_the_board_page_itself_can_still_write(self):
        host = f"127.0.0.1:{self.httpd.server_address[1]}"
        req = urllib.request.Request(
            self.base + "/api/task", data=json.dumps({"title": "fine"}).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Origin": f"http://{host}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

"""Local web board: HTTP API plus server-sent events, over the same SQLite file.

Stdlib only.  The board is the product — situational awareness is the whole
reason to prefer a board over a chat transcript — so it ships with the tool
rather than as a follow-on.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from dispatch import board as B
from dispatch import proposals as P
from dispatch import workflows as W
from dispatch.config import (
    load_agents,
    load_config,
    save_agents,
    save_config,
)
from dispatch.db import DB

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def snapshot(root: str, db: DB) -> dict[str, Any]:
    cfg = load_config(root)
    wfs = W.load(db)
    agents = load_agents(root)
    tasks = B.all_tasks(db)

    by_id = {t["id"]: t for t in tasks}
    edges = [dict(r) for r in db.q("SELECT src,dst,kind FROM edges")]
    leased = {r["task_id"] for r in db.q("SELECT task_id FROM leases")}

    cards = []
    for t in tasks:
        deps = [e["src"] for e in edges
                if e["dst"] == t["id"] and e["kind"] == "finish_to_start"]
        cards.append({
            **t,
            "running": t["id"] in leased,
            "deps": deps,
            "dep_titles": [{"id": d, "title": by_id[d]["title"],
                            "status": by_id[d]["status"]}
                           for d in deps if d in by_id],
            "blockers": B.blockers(db, cfg, wfs, t),
            "children": [c["id"] for c in B.children_of(db, t["id"])],
        })

    checkpoints = []
    for r in db.q("SELECT * FROM checkpoints WHERE status='open' ORDER BY created_at"):
        d = dict(r)
        d["bundle"] = json.loads(d.get("bundle") or "{}")
        t = by_id.get(d["task_id"])
        d["title"] = t["title"] if t else d["task_id"]
        checkpoints.append(d)

    ratio, created, done = P.expansion_ratio(db, cfg)
    spent = db.q1("SELECT COALESCE(SUM(usd),0) usd, COUNT(*) n FROM runs")

    return {
        "config": cfg,
        "stages": cfg["stages"],
        "workflows": wfs,
        "agents": agents,
        "tasks": cards,
        "edges": edges,
        "checkpoints": checkpoints,
        "proposals": P.pending(db),
        "scheduler": {
            "running": _sched_alive(root),
            "paused": bool(cfg["scheduler"].get("paused")),
            "in_flight": len(leased),
            "max_concurrent": cfg["scheduler"].get("max_concurrent"),
        },
        "stats": {
            "usd": round(float(spent["usd"] or 0), 2),
            "runs": spent["n"],
            "expansion_ratio": round(ratio, 2),
            "created_recent": created,
            "done_recent": done,
        },
        "validation": W.validate(wfs, cfg, agents),
    }


def _sched_alive(root: str) -> bool:
    from dispatch.scheduler import read_pid
    return read_pid(root) is not None


class Handler(BaseHTTPRequestHandler):
    root: str = ""
    db: DB | None = None
    server_version = "dispatch/0.1"

    def log_message(self, fmt, *args):  # quiet by default
        pass

    # -- helpers ------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True                      # curl, the CLI, same-origin GETs
        host = self.headers.get("Host") or ""
        return origin.split("//")[-1] == host

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/state":
                return self._json(snapshot(self.root, self.db))
            if p == "/api/events":
                return self._sse()
            if p.startswith("/api/task/"):
                tid = p.split("/")[-1]
                t = B.get(self.db, tid)
                if not t:
                    return self._json({"error": "no such card"}, 404)
                t["runs"] = [dict(r) for r in self.db.q(
                    "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC", (tid,))]
                t["gate_runs"] = [dict(r) for r in self.db.q(
                    "SELECT * FROM gate_runs WHERE task_id=? ORDER BY ts DESC LIMIT 40",
                    (tid,))]
                t["events"] = [dict(r) for r in self.db.q(
                    "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 60", (tid,))]
                return self._json(t)
            if p == "/api/workflows":
                return self._json({"card_types": W.load(self.db)})
            if p == "/api/workflows/export":
                payload = {"version": 1, "card_types": W.load(self.db)}
                body = json.dumps(payload, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 'attachment; filename="workflows.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body) and None
            if p == "/api/memory":
                from dispatch import memory as MEM
                q = (qs.get("q") or [""])[0]
                limit = int((qs.get("limit") or ["20"])[0])
                tags = [t for t in (qs.get("tags") or [""])[0].split(",") if t]
                found = (MEM.search(self.db, q, limit=limit, tags=tags) if q
                         else MEM.all_memories(self.db, limit=limit))
                return self._json({"memories": found, "query": q})
            if p.startswith("/api/memory/"):
                from dispatch import memory as MEM
                m = MEM.get(self.db, p.rsplit("/", 1)[-1])
                return self._json(m) if m else self._json({"error": "no such memory"}, 404)
            if p == "/api/blocked":
                cfg, wfs = load_config(self.root), W.load(self.db)
                out = []
                for t in B.all_tasks(self.db, include_terminal=False):
                    bl = B.blockers(self.db, cfg, wfs, t)
                    if bl:
                        out.append({"id": t["id"], "title": t["title"],
                                    "stage": t["stage"], "status": t["status"],
                                    "blockers": bl})
                return self._json({"blocked": out})
            if p == "/api/log":
                lim = int(qs.get("limit", ["200"])[0])
                rows = self.db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (lim,))
                return self._json({"events": [dict(r) for r in rows]})
            return self._json({"error": "not found"}, 404)
        except Exception:
            return self._json({"error": traceback.format_exc()[-2000:]}, 500)

    def do_POST(self) -> None:
        if not self._same_origin():
            return self._json({"error": "cross-origin writes are refused"}, 403)
        u = urlparse(self.path)
        p, body = u.path, self._body()
        cfg, wfs = load_config(self.root), W.load(self.db)
        try:
            if p == "/api/task":
                tid = B.create(self.db, cfg, wfs,
                               title=body.get("title", "(untitled)"),
                               brief=body.get("brief", ""),
                               card_type=body.get("card_type", "development"),
                               acceptance=body.get("acceptance") or [],
                               parent_id=body.get("parent_id") or None,
                               tags=body.get("tags") or [],
                               priority=int(body.get("priority", 50)),
                               scope=body.get("scope") or [],
                               depends_on=body.get("depends_on") or [],
                               budget=body.get("budget") or None)
                if body.get("start"):
                    B.start_card(self.db, wfs, tid)
                return self._json({"id": tid})

            if p.startswith("/api/task/") and p.endswith("/start"):
                B.start_card(self.db, wfs, p.split("/")[-2])
                return self._json({"ok": True})

            if p.startswith("/api/task/") and p.endswith("/cancel"):
                B.cancel(self.db, p.split("/")[-2])
                return self._json({"ok": True})

            if p.startswith("/api/task/") and p.endswith("/move"):
                tid = p.split("/")[-2]
                t = B.get(self.db, tid)
                if not t:
                    return self._json({"error": "no such card"}, 404)
                target = body.get("stage")
                # A human drag is a proposal with provenance:human, auto-accepted.
                B.update(self.db, tid, actor="human", stage=target, status=B.QUEUED,
                         attempts=0, defer_until=0, defer_reason=None,
                         block_reason=None,
                         agent_type=(W.stage_entry(wfs, t["card_type"], target) or {})
                         .get("agent") or t.get("agent_type"))
                self.db.emit("task.moved", tid, actor="human",
                             frm=t["stage"], to=target)
                return self._json({"ok": True})

            if p.startswith("/api/task/") and p.endswith("/edge"):
                tid = p.split("/")[-2]
                try:
                    B.link(self.db, body["src"], body.get("dst") or tid,
                           body.get("kind", "finish_to_start"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True})

            if p.startswith("/api/checkpoint/"):
                cid = p.split("/")[-2] if p.endswith("/respond") else p.split("/")[-1]
                B.resolve_checkpoint(self.db, cfg, wfs, cid,
                                     body.get("response", "approve"),
                                     body.get("note", ""))
                return self._json({"ok": True})

            if p.startswith("/api/proposal/"):
                pid = p.split("/")[-2]
                prop = self.db.q1("SELECT * FROM proposals WHERE id=?", (pid,))
                if not prop:
                    return self._json({"error": "no such proposal"}, 404)
                decision = body.get("decision", "accept")
                if decision == "accept":
                    P.apply_proposal(self.db, self.root, cfg, wfs, dict(prop),
                                     "human", body.get("note", "accepted by you"))
                else:
                    P._decide(self.db, dict(prop), "rejected", "human",
                              body.get("note", "rejected by you"))
                return self._json({"ok": True})

            if p == "/api/intent":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json({"error": "describe what you want"}, 400)
                if "intent" not in wfs:
                    return self._json({"error": "this board has no 'intent' "
                                                "card type — run dispatch "
                                                "upgrade --apply"}, 400)
                tid = B.create(self.db, cfg, wfs,
                               title=(body.get("title")
                                      or text.splitlines()[0][:72]),
                               brief=text, card_type="intent",
                               acceptance=["a plan you would approve"],
                               priority=int(body.get("priority", 70)),
                               provenance="human")
                B.start_card(self.db, wfs, tid)
                return self._json({"id": tid})
            if p == "/api/memory":
                from dispatch import memory as MEM
                if not body.get("title") or not body.get("body"):
                    return self._json({"error": "title and body are required"}, 400)
                mid = MEM.add(self.db, title=body["title"], body=body["body"],
                              tags=body.get("tags") or [],
                              kind=body.get("kind", "fact"),
                              source_task=body.get("source_task"),
                              actor=body.get("actor", "human"))
                return self._json({"id": mid})
            if p == "/api/scheduler":
                cfg["scheduler"]["paused"] = bool(body.get("paused"))
                save_config(self.root, cfg)
                self.db.emit("scheduler.paused" if body.get("paused")
                             else "scheduler.resumed", actor="human")
                return self._json({"ok": True})

            if p == "/api/workflows/import":
                wf = body.get("card_types") or body
                W.save(self.db, wf, actor="human")
                W.export_file(self.root, self.db)
                return self._json({"ok": True,
                                   "problems": W.validate(wf, cfg,
                                                          load_agents(self.root))})
            return self._json({"error": "not found"}, 404)
        except Exception:
            return self._json({"error": traceback.format_exc()[-2000:]}, 500)

    def do_DELETE(self) -> None:
        if not self._same_origin():
            return self._json({"error": "cross-origin writes are refused"}, 403)
        p = urlparse(self.path).path
        if p.startswith("/api/memory/"):
            from dispatch import memory as MEM
            ok = MEM.delete(self.db, p.rsplit("/", 1)[-1])
            return self._json({"ok": ok}, 200 if ok else 404)
        return self._json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        if not self._same_origin():
            return self._json({"error": "cross-origin writes are refused"}, 403)
        u = urlparse(self.path)
        p, body = u.path, self._body()
        cfg = load_config(self.root)
        try:
            if p == "/api/workflows":
                wf = body.get("card_types") or body
                W.save(self.db, wf, actor="human")
                W.export_file(self.root, self.db)
                return self._json({"ok": True,
                                   "problems": W.validate(wf, cfg,
                                                          load_agents(self.root))})
            if p == "/api/config":
                save_config(self.root, {**cfg, **body})
                return self._json({"ok": True})
            if p == "/api/agents":
                save_agents(self.root, body)
                return self._json({"ok": True})
            if p.startswith("/api/memory/"):
                from dispatch import memory as MEM
                ok = MEM.update(self.db, p.rsplit("/", 1)[-1],
                                title=body.get("title"), body=body.get("body"),
                                tags=body.get("tags"), kind=body.get("kind"))
                return self._json({"ok": ok}, 200 if ok else 404)
            if p.startswith("/api/task/"):
                tid = p.split("/")[-1]
                allowed = {"title", "brief", "acceptance", "tags", "priority",
                           "card_type", "max_attempts", "gates", "budget",
                           "block_reason", "status", "agent_type"}
                fields = {k: v for k, v in body.items() if k in allowed}
                if "scope" in body:
                    t = B.get(self.db, tid)
                    ws = dict((t or {}).get("workspace") or {})
                    ws["scope"] = body["scope"]
                    fields["workspace"] = ws
                B.update(self.db, tid, actor="human", **fields)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception:
            return self._json({"error": traceback.format_exc()[-2000:]}, 500)

    # -- sse ----------------------------------------------------------------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)

        def cb(ev):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

        self.db.subscribe(cb)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    payload = json.dumps(ev, default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.db.unsubscribe(cb)

    def _static(self, rel: str) -> None:
        """Serve a file from web/ and nowhere else.

        Containment is a resolved-path check, not string surgery: stripping
        ".." leaves an absolute path intact, and os.path.join returns it.
        """
        web = os.path.realpath(WEB_DIR)
        path = os.path.realpath(os.path.join(web, rel.lstrip("/")))
        inside = path == web or path.startswith(web + os.sep)
        if not inside or not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)


class Board(ThreadingHTTPServer):
    """`HTTPServer.server_bind` calls `socket.getfqdn()`, which on some machines
    blocks for half a minute. That delay used to sit between the daemon starting
    and the first card dispatching — the board is secondary, and it must never
    be able to hold up the loop."""

    allow_reuse_address = True

    def server_bind(self):
        from socketserver import TCPServer
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(root: str, db: DB, host: str = "127.0.0.1", port: int = 7777,
          block: bool = True) -> ThreadingHTTPServer:
    Handler.root = root
    Handler.db = db
    httpd = Board((host, port), Handler)
    httpd.daemon_threads = True
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

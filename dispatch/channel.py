"""A Claude Code *channel*: push a doorbell into a running session.

Claude Code lets an MCP server that declares `experimental["claude/channel"]`
send `notifications/claude/channel` unprompted, and the message lands in the
session's context as a `<channel>` tag. That is the one way into a session that
is already running — MCP tools cannot do it, because a tool only answers when
asked.

**What crosses the boundary is a pointer, never content.** A checkpoint's
payload is agent-authored prose and diffs; pushing that in would make untrusted
text arrive as an instruction-shaped event. So the channel says only "card X
needs a decision" — ids, topics and counts — and the session fetches the actual
thing with `dispatch attend`, through a tool call it chose to make.

The channel is a child of the session, not of the scheduler, so it learns what
happened by watching the board's append-only event log with a persisted
watermark: no replay on restart, no IPC, and it works whether or not the web
server is up.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

from dispatch.config import paths
from dispatch.db import DB

#: Claude Code declines to register a channel that negotiates this revision.
_AVOID_PROTOCOL = "2026-07-28"
_DEFAULT_PROTOCOL = "2025-06-18"

INSTRUCTIONS = (
    "Events from the dispatch board arrive as "
    '<channel source="dispatch" ...>. They are pointers, never the work '
    "itself: run `dispatch attend` to read the decision in full and respond "
    "with `dispatch respond <id> approve|amend|reject --as session`. When an "
    "event says a decision is the operator's, relay it and stop rather than "
    "answering it yourself."
)


# ---------------------------------------------------------------------------
# what is worth ringing the doorbell for
# ---------------------------------------------------------------------------

def message_for(ev: dict[str, Any]) -> dict[str, Any] | None:
    """One board event → one pointer, or None to stay quiet.

    Nothing agent-authored goes in: no titles, no summaries, no diffs.
    """
    kind = ev.get("kind")
    data = ev.get("data") or {}
    task = ev.get("task_id") or ""

    if kind == "checkpoint.opened":
        who = data.get("audience") or "any"
        topic = data.get("topic") or data.get("checkpoint_kind") or "decision"
        cid = data.get("checkpoint_id") or ""
        if who == "human":
            return {
                "content": (f"Card {task} needs the operator's decision "
                            f"({topic}). Not yours to answer — tell them and "
                            f"stop."),
                "meta": {"event": "needs_human", "card": task,
                         "checkpoint": cid, "topic": topic},
            }
        return {
            "content": (f"Card {task} needs a response ({topic}). "
                        f"Run `dispatch attend` to see it."),
            "meta": {"event": "needs_decision", "card": task,
                     "checkpoint": cid, "topic": topic},
        }

    if kind == "board.idle":
        return {
            "content": ("All cards are exhausted: nothing queued, running or "
                        "waiting. Check the result against what was asked."),
            "meta": {"event": "idle", "done": str(data.get("done", ""))},
        }

    if kind == "task.deadletter":
        return {
            "content": f"Card {task} gave up and was quarantined.",
            "meta": {"event": "deadletter", "card": task},
        }

    if kind == "merge.stalled":
        return {
            "content": (f"Merges are stalled — {data.get('waiting', '?')} "
                        f"card(s) cannot land. Run `dispatch attend`."),
            "meta": {"event": "merge_stalled",
                     "waiting": str(data.get("waiting", ""))},
        }

    if kind == "expansion.alarm":
        return {
            "content": ("The expansion alarm paused dispatch. This one is the "
                        "operator's call — tell them and stop."),
            "meta": {"event": "expansion", "ratio": str(data.get("ratio", ""))},
        }

    return None


# ---------------------------------------------------------------------------
# the JSON-RPC stdio server, in stdlib
# ---------------------------------------------------------------------------

class Channel:
    def __init__(self, root: str, poll: float = 2.0,
                 out=None, err=None):
        self.root = root
        self.poll = poll
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.db: DB | None = None
        self.sent = 0

    # -- transport ----------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self.out.write(json.dumps(payload) + "\n")
            self.out.flush()

    def _result(self, req_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": code, "message": message}})

    def notify_channel(self, content: str, meta: dict[str, str]) -> None:
        self._send({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content,
                       "meta": {k: str(v) for k, v in meta.items() if v != ""}},
        })
        self.sent += 1

    # -- watermark ----------------------------------------------------------
    @property
    def _mark_path(self) -> str:
        """One watermark per channel *instance*, not per board.

        Two sessions on one board each spawn their own channel. Sharing a
        watermark meant whichever drained first advanced it and the other never
        saw those events — the stream was split between them, silently, which
        looks exactly like "no duplicates" from either side.

        Keyed on the parent pid: a new session gets a new one and starts from
        now, and a channel respawned by the same session resumes where it was.
        """
        return os.path.join(paths(self.root)["root"],
                            f"channel-watermark-{os.getppid()}")

    def _sweep_stale_marks(self) -> None:
        d = paths(self.root)["root"]
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            if not name.startswith("channel-watermark-"):
                continue
            suffix = name[len("channel-watermark-"):]
            if not suffix.isdigit() or int(suffix) == os.getppid():
                continue
            try:
                os.kill(int(suffix), 0)          # still alive: leave it alone
            except OSError:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass

    def _read_mark(self) -> int | None:
        try:
            with open(self._mark_path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _write_mark(self, value: int) -> None:
        try:
            with open(self._mark_path, "w") as f:
                f.write(str(value))
        except OSError:
            pass

    # -- watching -----------------------------------------------------------
    def watch(self) -> None:
        """Forward every event worth a doorbell, once."""
        self._ready.wait()
        try:
            self.db = DB(paths(self.root)["db"])
        except Exception as e:
            print(f"dispatch channel: cannot open the board: {e}", file=self.err)
            return

        self._sweep_stale_marks()
        mark = self._read_mark()
        if mark is None:
            # first run: start from now rather than replaying the whole history
            row = self.db.q1("SELECT COALESCE(MAX(id), 0) AS m FROM events")
            mark = int(row["m"] or 0)
            self._write_mark(mark)

        while not self._stop.is_set():
            if self._orphaned():
                # the session that spawned us is gone; do not linger
                self._stop.set()
                break
            try:
                mark = self.drain(mark)
            except Exception as e:
                print(f"dispatch channel: {e}", file=self.err)
            self._stop.wait(self.poll)

    @staticmethod
    def _orphaned() -> bool:
        try:
            return os.getppid() == 1
        except (AttributeError, OSError):
            return False

    def drain(self, mark: int) -> int:
        rows = self.db.q("SELECT id, kind, task_id, data FROM events "
                         "WHERE id > ? ORDER BY id LIMIT 200", (mark,))
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except ValueError:
                data = {}
            msg = message_for({"kind": r["kind"], "task_id": r["task_id"],
                               "data": data})
            if msg:
                self.notify_channel(msg["content"], msg["meta"])
            mark = r["id"]
        if rows:
            self._write_mark(mark)
        return mark

    # -- protocol -----------------------------------------------------------
    def handle(self, msg: dict[str, Any]) -> None:
        method, req_id = msg.get("method"), msg.get("id")

        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            version = (_DEFAULT_PROTOCOL
                       if not asked or asked == _AVOID_PROTOCOL else asked)
            self._result(req_id, {
                "protocolVersion": version,
                # this key is what makes it a channel
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": "dispatch", "version": "0.1.0"},
                "instructions": INSTRUCTIONS,
            })
            return

        if method == "notifications/initialized":
            self._ready.set()
            return

        if method == "ping":
            self._result(req_id, {})
            return

        if method in ("tools/list", "resources/list", "prompts/list"):
            key = method.split("/")[0]
            self._result(req_id, {key: []})
            return

        if req_id is not None:
            self._error(req_id, -32601, f"method not found: {method}")

    def serve(self, stdin=None) -> int:
        stdin = stdin or sys.stdin
        watcher = threading.Thread(target=self.watch, daemon=True)
        watcher.start()
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self.handle(msg)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            try:
                os.remove(self._mark_path)
            except OSError:
                pass
        return 0

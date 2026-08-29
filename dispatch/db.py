"""SQLite store plus the append-only event log.

Board state is a normal set of tables; every state change also appends to
`events`, which is what makes "what did the arbiter see when it approved that?"
answerable on day three.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes; ids get typed by hand


def new_id(prefix: str, n: int = 6) -> str:
    return prefix + "_" + "".join(secrets.choice(_ALPHABET) for _ in range(n))


def now() -> float:
    return time.time()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    brief         TEXT NOT NULL DEFAULT '',
    acceptance    TEXT NOT NULL DEFAULT '[]',   -- json: list of checks/prose
    card_type     TEXT NOT NULL DEFAULT 'development',
    stage         TEXT NOT NULL DEFAULT 'backlog',
    agent_type    TEXT,                          -- resolved from workflow, overridable
    model         TEXT,                          -- overrides the stage and the agent
    status        TEXT NOT NULL DEFAULT 'queued',
    parent_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    priority      INTEGER NOT NULL DEFAULT 50,
    tags          TEXT NOT NULL DEFAULT '[]',
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    budget        TEXT NOT NULL DEFAULT '{}',    -- json {usd, tokens, wall_clock_s}
    spent         TEXT NOT NULL DEFAULT '{}',
    workspace     TEXT NOT NULL DEFAULT '{}',    -- json {worktree, branch, base_ref, scope}
    artifacts     TEXT NOT NULL DEFAULT '[]',
    gates         TEXT NOT NULL DEFAULT '[]',    -- task-level gate overrides
    provenance    TEXT NOT NULL DEFAULT 'human',
    proposal_id   TEXT,
    defer_until   REAL NOT NULL DEFAULT 0,
    defer_reason  TEXT,
    block_reason  TEXT,
    last_evidence TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    started_at    REAL,
    completed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_stage  ON tasks(stage);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

-- Three edge kinds, deliberately distinct:
--   finish_to_start : ordinary ordering
--   artifact        : dst consumes src's output; scheduler injects it into context
--   mutex           : not ordering — these two may never run concurrently
CREATE TABLE IF NOT EXISTS edges (
    id   TEXT PRIMARY KEY,
    src  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dst  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'finish_to_start',
    note TEXT,
    UNIQUE(src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);

CREATE TABLE IF NOT EXISTS leases (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    run_id       TEXT NOT NULL,
    pid          INTEGER,
    stage        TEXT,
    heartbeat_at REAL NOT NULL,
    expires_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    task_id TEXT,
    kind    TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT 'scheduler',
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    agent_type   TEXT NOT NULL,
    model        TEXT,
    attempt      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'running',
    exit_code    INTEGER,
    summary      TEXT,
    usd          REAL,
    duration_s   REAL,
    log_dir      TEXT,
    started_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS arbiter_calls (
    id           TEXT PRIMARY KEY,
    -- nullable on purpose: adjudicating a proposal that came from no card
    -- still costs money, and dropping those is how the total drifts
    task_id      TEXT,
    purpose      TEXT NOT NULL,      -- judge_acceptance | adjudicate | triage
    model        TEXT,
    outcome      TEXT NOT NULL,      -- ok | unconfigured | unreachable | unreadable
    usd          REAL,
    duration_s   REAL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arbiter_task ON arbiter_calls(task_id);

CREATE TABLE IF NOT EXISTS gate_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    run_id       TEXT,
    gate         TEXT NOT NULL,
    hook         TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    reason       TEXT,
    retry_after_s REAL,
    evidence     TEXT,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gateruns_task ON gate_runs(task_id, ts);

CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    from_task   TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    rationale   TEXT,
    confidence  REAL,
    urgency     TEXT NOT NULL DEFAULT 'normal',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|accepted|modified|rejected|escalated
    tier        TEXT,                             -- policy|arbiter|human
    decision    TEXT,
    decided_by  TEXT,
    decided_at  REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS checkpoints (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'signoff',  -- signoff | escalation
    audience      TEXT NOT NULL DEFAULT 'any',      -- any | session | human
    topic         TEXT,                             -- the gate or reason behind it
    question      TEXT NOT NULL,
    bundle        TEXT NOT NULL DEFAULT '{}',   -- diff, tests, summary, options
    status        TEXT NOT NULL DEFAULT 'open', -- open|approved|rejected|amended|expired
    response      TEXT,
    response_note TEXT,
    sla_s         REAL,
    created_at    REAL NOT NULL,
    resolved_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON checkpoints(status);

CREATE TABLE IF NOT EXISTS locks (
    name        TEXT PRIMARY KEY,
    task_id     TEXT,
    acquired_at REAL
);

-- What agents have learned about this repo, so the next one starts warm.
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    kind        TEXT NOT NULL DEFAULT 'fact',
    source_task TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at);

CREATE TABLE IF NOT EXISTS workflows (
    card_type  TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#6B7A75',
    stages     TEXT NOT NULL DEFAULT '[]',   -- ordered list of {stage, agent, gates, lock, ...}
    updated_at REAL NOT NULL
);
"""


class DB:
    """Thread-safe SQLite handle. One connection, one lock — the write volume
    here is trivial and a single writer removes a whole class of bug."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()
        self._listeners: list[Any] = []

    def _migrate(self) -> None:
        """Additive column migrations for boards created by an earlier build."""
        for table, col, decl in (
            ("checkpoints", "kind", "TEXT NOT NULL DEFAULT 'signoff'"),
            ("tasks", "plan", "TEXT"),
            ("checkpoints", "audience", "TEXT NOT NULL DEFAULT 'any'"),
            ("tasks", "model", "TEXT"),
            ("runs", "model", "TEXT"),
            ("checkpoints", "topic", "TEXT"),
        ):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # -- primitives ---------------------------------------------------------
    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def q1(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            self.conn.commit()
            return cur

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- event log ----------------------------------------------------------
    def emit(self, kind: str, task_id: str | None = None, /, *,
             actor: str = "scheduler", **data: Any) -> None:
        """`kind` and `task_id` are positional-only so an event payload may
        legitimately carry its own `kind` field without colliding with this
        signature — which it does, constantly, for proposals."""
        ts = now()
        self.x(
            "INSERT INTO events (ts, task_id, kind, actor, data) VALUES (?,?,?,?,?)",
            (ts, task_id, kind, actor, json.dumps(data, default=str)),
        )
        payload = {"ts": ts, "task_id": task_id, "kind": kind,
                   "actor": actor, "data": data}
        for cb in list(self._listeners):
            try:
                cb(payload)
            except Exception:
                pass

    def subscribe(self, cb) -> None:
        self._listeners.append(cb)

    def unsubscribe(self, cb) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def row_to_task(r: sqlite3.Row) -> dict[str, Any]:
    """Decode the JSON columns so callers see real Python objects."""
    d = dict(r)
    for k in ("acceptance", "tags", "artifacts", "gates"):
        d[k] = json.loads(d.get(k) or "[]")
    for k in ("budget", "spent", "workspace"):
        d[k] = json.loads(d.get(k) or "{}")
    if d.get("plan"):
        try:
            d["plan"] = json.loads(d["plan"])
        except (ValueError, TypeError):
            pass
    return d


def open_db(root: str) -> DB:
    from dispatch import DISPATCH_DIR
    path = os.path.join(root, DISPATCH_DIR, "board.db")
    if not os.path.exists(path):
        raise SystemExit(
            "no board here — run `dispatch init` at the root of the repo you "
            "want to orchestrate."
        )
    return DB(path)

"""What the agents already know about this repo.

Every card starts a fresh agent with no history, so the first minutes of each
run are spent rediscovering the same things: where the tests live, which module
owns what, the gotcha that bit the last three cards. This is where that goes,
once, so the next agent starts warm.

Search is SQLite's FTS5 with bm25 ranking — no dependency, no model, no daemon.
A vector store would rank paraphrases better; it would also add a large
dependency tree to a tool that currently has none, and repo facts are mostly
retrieved by their nouns (file names, symbols, commands), which is exactly what
keyword search is good at. If recall turns out to be the limit, the retrieval
call is one function and can be swapped.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from dispatch.db import DB, new_id, now

KINDS = ("fact", "convention", "gotcha", "pointer", "decision")


def _fts_ok(db: DB) -> bool:
    return db.get_meta("memory_fts", "0") == "1"


def ensure(db: DB) -> None:
    """Create the search index if this SQLite build has FTS5. Called on open;
    a build without it still works, just with weaker matching."""
    if _fts_ok(db):
        return
    try:
        with db._lock:
            db.conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(title, body, tags, content='memories',
                           content_rowid='rowid');
            """)
            db.conn.commit()
        db.set_meta("memory_fts", "1")
    except sqlite3.OperationalError:
        db.set_meta("memory_fts", "0")


def _reindex(db: DB, rowid: int, title: str, body: str, tags: str) -> None:
    if not _fts_ok(db):
        return
    try:
        db.x("INSERT INTO memories_fts (rowid, title, body, tags) "
             "VALUES (?,?,?,?)", (rowid, title, body, tags))
    except sqlite3.OperationalError:
        pass


def _unindex(db: DB, rowid: int, title: str, body: str, tags: str) -> None:
    if not _fts_ok(db):
        return
    try:
        db.x("INSERT INTO memories_fts (memories_fts, rowid, title, body, tags) "
             "VALUES ('delete', ?,?,?,?)", (rowid, title, body, tags))
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def add(db: DB, *, title: str, body: str, tags: list[str] | None = None,
        kind: str = "fact", source_task: str | None = None,
        actor: str = "human") -> str:
    ensure(db)
    existing = db.q1("SELECT id FROM memories WHERE lower(title)=lower(?)",
                     (title.strip(),))
    if existing:
        update(db, existing["id"], body=body, tags=tags, kind=kind, actor=actor)
        return existing["id"]

    mid = new_id("m")
    ts = now()
    tag_s = " ".join(tags or [])
    cur = db.x("INSERT INTO memories (id,title,body,tags,kind,source_task,"
               "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
               (mid, title.strip(), body.strip(), json.dumps(tags or []),
                kind if kind in KINDS else "fact", source_task, ts, ts))
    _reindex(db, cur.lastrowid, title, body, tag_s)
    db.emit("memory.written", source_task, actor=actor, memory_id=mid,
            title=title[:120])
    return mid


def update(db: DB, mid: str, *, title: str | None = None,
           body: str | None = None, tags: list[str] | None = None,
           kind: str | None = None, actor: str = "human") -> bool:
    row = db.q1("SELECT rowid, * FROM memories WHERE id=?", (mid,))
    if not row:
        return False
    _unindex(db, row["rowid"], row["title"], row["body"], row["tags"])
    new_title = title if title is not None else row["title"]
    new_body = body if body is not None else row["body"]
    new_tags = json.dumps(tags) if tags is not None else row["tags"]
    new_kind = kind if kind in KINDS else row["kind"]
    db.x("UPDATE memories SET title=?, body=?, tags=?, kind=?, updated_at=? "
         "WHERE id=?", (new_title, new_body, new_tags, new_kind, now(), mid))
    _reindex(db, row["rowid"], new_title, new_body,
             " ".join(json.loads(new_tags or "[]")))
    db.emit("memory.updated", actor=actor, memory_id=mid)
    return True


def delete(db: DB, mid: str, actor: str = "human") -> bool:
    row = db.q1("SELECT rowid, * FROM memories WHERE id=?", (mid,))
    if not row:
        return False
    _unindex(db, row["rowid"], row["title"], row["body"], row["tags"])
    db.x("DELETE FROM memories WHERE id=?", (mid,))
    db.emit("memory.deleted", actor=actor, memory_id=mid)
    return True


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def get(db: DB, mid: str) -> dict[str, Any] | None:
    row = db.q1("SELECT * FROM memories WHERE id=?", (mid,))
    return _row(row) if row else None


def all_memories(db: DB, limit: int = 200) -> list[dict[str, Any]]:
    return [_row(r) for r in db.q(
        "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,))]


_FTS_SAFE = re.compile(r"[A-Za-z0-9_./-]+")

#: Words that match everything and therefore mean nothing. Left in an OR query
#: they pull in every memory in the store — a card about a rate limiter was
#: handed the office wifi password because both contained "the".
_STOP = {
    "a", "an", "the", "and", "or", "but", "if", "then", "of", "in", "on", "at",
    "to", "for", "from", "with", "by", "as", "is", "it", "its", "be", "are",
    "was", "were", "this", "that", "these", "those", "we", "you", "i", "not",
    "no", "so", "do", "does", "did", "can", "will", "should", "would", "use",
    "using", "used", "add", "fix", "make", "set", "get", "run", "new", "into",
    "when", "what", "which", "how", "why", "all", "any", "some", "more",
}


def terms_in(text: str) -> set:
    return {t.lower() for t in _FTS_SAFE.findall(text or "")
            if len(t) > 2 and t.lower() not in _STOP}


def _fts_query(text: str) -> str:
    """FTS5 syntax is a minefield of operators; quote every term so a card
    title full of punctuation cannot become a syntax error."""
    terms = sorted(terms_in(text))
    return " OR ".join('"' + t.replace('"', '') + '"' for t in terms[:40])


def search(db: DB, query: str, limit: int = 8,
           tags: list[str] | None = None) -> list[dict[str, Any]]:
    ensure(db)
    rows: list[Any] = []
    if _fts_ok(db) and query.strip():
        q = _fts_query(query)
        if q:
            try:
                rows = db.q(
                    "SELECT m.*, bm25(memories_fts) AS score FROM memories_fts "
                    "JOIN memories m ON m.rowid = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY score LIMIT ?",
                    (q, limit * 3))
            except sqlite3.OperationalError:
                rows = []
    if not rows:
        like = f"%{(query or '').strip()}%"
        rows = db.q("SELECT * FROM memories WHERE title LIKE ? OR body LIKE ? "
                    "ORDER BY updated_at DESC LIMIT ?", (like, like, limit * 3))

    # A memory that shares no meaningful word with the query is noise, and
    # injecting noise spends exactly the tokens this is meant to save.
    asked = terms_in(query)
    out = []
    want = set(tags or [])
    for r in rows:
        m = _row(r)
        if want and not (want & set(m["tags"])):
            continue
        if asked:
            has = terms_in(m["title"] + " " + m["body"] + " " + " ".join(m["tags"]))
            if not (asked & has):
                continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _row(r) -> dict[str, Any]:
    d = dict(r)
    d.pop("rowid", None)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


# ---------------------------------------------------------------------------
# what an agent is handed
# ---------------------------------------------------------------------------

def brief_for(db: DB, task: dict[str, Any], limit: int = 8,
              budget_chars: int = 2400) -> str:
    """The memories most likely to save this card's agent a rediscovery.

    Injected rather than searched, because an agent that has to remember to
    look things up mostly does not."""
    query = " ".join(filter(None, [
        task.get("title", ""), (task.get("brief") or "")[:600],
        " ".join(task.get("tags") or []),
        " ".join((task.get("workspace") or {}).get("scope") or []),
    ]))
    hits = search(db, query, limit=limit)
    if not hits:
        return ""
    lines, used = [], 0
    for m in hits:
        entry = f"- **{m['title']}** ({m['kind']}) — {m['body'].strip()}"
        if used + len(entry) > budget_chars:
            break
        lines.append(entry)
        used += len(entry)
    if not lines:
        return ""
    return "\n".join(lines)


def usage_note(board_url: str | None) -> str:
    """Told to every agent, so what one learns the next one starts with."""
    http = ""
    if board_url:
        http = (f"\n\nOver HTTP, if you prefer: `GET {board_url}/api/memory?q=…`"
                f" and `POST {board_url}/api/memory` "
                f"with `{{\"title\":…, \"body\":…, \"tags\":[…], \"kind\":…}}`.")
    return (
        "Shared memory holds what earlier agents learned about this repo, so "
        "you do not rediscover it.\n\n"
        "```\n"
        "dispatch memory search \"rate limiter\"      # before you go digging\n"
        "dispatch memory add \"Where the API tests live\" \\\n"
        "  --body \"tests/api/, run with `npm test -- api`. Fixtures in conftest.\" \\\n"
        "  --tags api,testing --kind pointer\n"
        "```\n\n"
        "Write one when you learn something durable and non-obvious that the "
        "next card would otherwise have to work out again — where something "
        "lives, a convention, a gotcha, why a decision went the way it did. "
        "Do not write down what the code already says, anything specific to "
        "this one card, or anything you have not verified."
        + http)

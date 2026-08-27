"""The event log and the store."""
from dispatch.db import DB, new_id
from tests.helpers import BoardCase


class TestEventLog(BoardCase):
    needs_git = False

    def test_emit_accepts_a_payload_field_named_kind(self):
        # REGRESSION: `kind` and `task_id` were ordinary parameters, so any
        # event carrying its own `kind` field -- which every proposal event
        # does -- raised TypeError and took out the adjudicator.
        self.db.emit("proposal.accepted", "t_x", actor="policy",
                     kind="add_task", task_id="payload-value")
        row = self.db.q1("SELECT kind, task_id, data FROM events "
                         "WHERE kind='proposal.accepted'")
        self.assertEqual(row["kind"], "proposal.accepted")
        self.assertEqual(row["task_id"], "t_x")
        self.assertIn("add_task", row["data"])

    def test_subscribers_see_events_and_a_broken_one_is_harmless(self):
        seen = []
        self.db.subscribe(lambda ev: seen.append(ev["kind"]))
        self.db.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
        self.db.emit("test.event", actor="test")
        self.assertEqual(seen, ["test.event"])

    def test_ids_avoid_look_alike_characters(self):
        # ids get typed by hand at a terminal
        for _ in range(200):
            self.assertFalse(set(new_id("t")[2:]) & set("ilo01"))

    def test_migration_adds_missing_columns(self):
        import os
        import sqlite3
        p = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(p)
        con.executescript("""
            CREATE TABLE checkpoints (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, question TEXT NOT NULL,
                bundle TEXT, status TEXT, response TEXT, response_note TEXT,
                sla_s REAL, created_at REAL, resolved_at REAL);
            INSERT INTO checkpoints (id, task_id, question, created_at)
            VALUES ('c_old', 't_old', 'legacy row', 0);
        """)
        con.commit()
        con.close()
        db = DB(p)
        try:
            row = db.q1("SELECT kind FROM checkpoints WHERE id='c_old'")
            self.assertEqual(row["kind"], "signoff")
        finally:
            db.close()

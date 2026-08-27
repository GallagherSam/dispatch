"""The loop.

A plain tick loop that never forms an opinion about whether the work is
finished.  It asks one question — is the ready set empty? — and if it isn't, it
dispatches.  There is no point in this file where a model's decision to stop
talking halts anything.
"""
from __future__ import annotations

import os
import threading
import traceback
from typing import Any

from dispatch import board as B
from dispatch import gates as G
from dispatch import proposals as P
from dispatch import workflows as W
from dispatch.config import load_config, paths
from dispatch.db import DB, now, row_to_task


def _sla_seconds(spec: Any) -> float | None:
    """Accepts seconds, or a readable "4h" / "30m" / "2d"."""
    if spec in (None, "", 0):
        return None
    if isinstance(spec, (int, float)):
        return float(spec)
    text = str(spec).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in units:
        try:
            return float(text[:-1]) * units[text[-1]]
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


class Scheduler:
    def __init__(self, root: str, db: DB, log=None):
        self.root = root
        self.db = db
        self.paths = paths(root)
        self.log = log or (lambda *a: None)
        self.stop_flag = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._results: list[dict[str, Any]] = []
        self._results_lock = threading.Lock()
        # Merges are serialised by construction: at most one in flight, ever.
        self._merging: str | None = None
        self._merge_thread: threading.Thread | None = None
        self._merge_result: dict[str, Any] | None = None
        self._merge_busy_streak = 0
        self._landed: set = set()      # cards checked and confirmed landed
        self._was_idle: bool | None = None
        self.cfg = load_config(root)
        self.workflows = W.load(db)
        self.ticks = 0

    def _check_sandbox(self, fatal: bool = False) -> bool:
        """A security control must never degrade silently."""
        from dispatch import sandbox as SB
        ok, problems = SB.preflight(self.cfg, self.root)
        for p in problems:
            self.log(p)
            self.db.emit("sandbox.problem", actor="scheduler", detail=p)
        if SB.enabled(self.cfg) and ok:
            self.log("sandbox: " + SB.describe(self.cfg))
        if not ok and fatal:
            raise SystemExit(
                "refusing to start: the sandbox is enabled but not usable.\n  "
                + "\n  ".join(problems))
        return ok

    # -- context for gates --------------------------------------------------
    def _ctx(self, task: dict[str, Any], **extra: Any) -> dict[str, Any]:
        ctx = {"db": self.db, "cfg": self.cfg, "workflows": self.workflows,
               "root": self.root, "paths": self.paths, "task": task}
        ctx.update(extra)
        return ctx

    # -- main loop ----------------------------------------------------------
    def run_forever(self) -> None:
        self.log(f"scheduler up — root={self.root}")
        self._check_sandbox(fatal=True)
        self.db.emit("scheduler.up", actor="scheduler", root=self.root)
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception:
                self.log("tick failed:\n" + traceback.format_exc())
                self.db.emit("scheduler.error", actor="scheduler",
                             error=traceback.format_exc()[-2000:])
            self.stop_flag.wait(self.cfg["scheduler"].get("tick_seconds", 5.0))
        self.db.emit("scheduler.down", actor="scheduler")
        self.log("scheduler down")

    def tick(self) -> None:
        self.ticks += 1
        self.cfg = load_config(self.root)
        self.workflows = W.load(self.db)

        self.reap_leases()
        self.expire_checkpoints()
        self.collect_finished()
        self.collect_merge()
        self.verify_landed()
        self.adjudicate()
        self.check_expansion()

        if self.cfg["scheduler"].get("paused"):
            return
        self.start_merge()
        self.dispatch()
        self.note_idle()

    # -- reaping ------------------------------------------------------------
    def reap_leases(self) -> None:
        """A lapsed lease is also the crash-recovery story: requeue and move on."""
        for r in self.db.q("SELECT * FROM leases WHERE expires_at < ?", (now(),)):
            tid = r["task_id"]
            if tid in self._threads and self._threads[tid].is_alive():
                self.db.x("UPDATE leases SET heartbeat_at=?, expires_at=? WHERE task_id=?",
                          (now(), now() + self.cfg["scheduler"]["lease_seconds"], tid))
                continue
            self.db.x("DELETE FROM leases WHERE task_id=?", (tid,))
            t = B.get(self.db, tid)
            if t and t["status"] in (B.LEASED, B.RUNNING):
                B.update(self.db, tid, actor="scheduler", status=B.QUEUED)
                self.db.emit("lease.expired", tid, actor="scheduler")
                self.log(f"reaped stale lease on {tid}")

    def expire_checkpoints(self) -> None:
        """An unanswered checkpoint should park its subtree cleanly rather than
        holding it open forever. What "park" means is the stage's call."""
        for r in self.db.q("SELECT * FROM checkpoints WHERE status='open' "
                           "AND sla_s IS NOT NULL AND sla_s > 0"):
            if now() - r["created_at"] < r["sla_s"]:
                continue
            tid = r["task_id"]
            task = B.get(self.db, tid)
            if not task:
                continue
            entry = W.stage_entry(self.workflows, task["card_type"],
                                  task["stage"]) or {}
            action = entry.get("on_sla", "block")
            hours = r["sla_s"] / 3600.0
            note = (f"no answer within {hours:.1f}h — "
                    f"the stage's on_sla rule is '{action}'")

            if action in ("approve", "reject"):
                B.resolve_checkpoint(self.db, self.cfg, self.workflows, r["id"],
                                     action, note=note, actor="sla")
            else:
                self.db.x("UPDATE checkpoints SET status='expired', "
                          "response_note=?, resolved_at=? WHERE id=?",
                          (note, now(), r["id"]))
                if task["status"] == B.CHECKPOINT:
                    B.update(self.db, tid, actor="sla", status=B.BLOCKED,
                             block_reason=note)
            self.db.emit("checkpoint.expired", tid, actor="sla",
                         checkpoint_id=r["id"], action=action,
                         sla_s=r["sla_s"])
            self.log(f"{tid} checkpoint expired after {hours:.1f}h -> {action}")

    def note_idle(self) -> None:
        """Emit once on the transition, not every tick — the interesting thing
        is the board *becoming* idle."""
        busy = self.db.q1(
            "SELECT COUNT(*) c FROM tasks WHERE status IN "
            "('queued','ready','leased','running','merging','blocked')")["c"]
        waiting = self.db.q1(
            "SELECT COUNT(*) c FROM checkpoints WHERE status='open'")["c"]
        idle = not busy and not waiting
        if idle != self._was_idle:
            if idle and self._was_idle is not None:
                done = self.db.q1(
                    "SELECT COUNT(*) c FROM tasks WHERE status='done'")["c"]
                self.db.emit("board.idle", actor="scheduler", done=done)
                self.log("board idle — nothing queued, running or waiting")
            self._was_idle = idle

    # -- merging ------------------------------------------------------------
    def verify_landed(self) -> None:
        """A card the board calls `done` must have nothing left on its branch.

        Every other failure here is loud; this one presents as success, and the
        work ends up on a branch nobody looks at again. So it is checked rather
        than assumed, on whatever path got the card to `done`.
        """
        from dispatch import merge as M
        if not self.cfg["runner"].get("merge_on_done", True):
            return
        every = max(1, int(self.cfg["runner"].get("verify_landed_every_ticks", 20)))
        if every > 1 and self.ticks % every != 1:
            return

        rows = self.db.q(
            "SELECT * FROM tasks WHERE stage='done' AND status IN (?,?,?)",
            (B.DONE, B.QUEUED, B.READY))
        for r in rows:
            task = row_to_task(r)
            if task["id"] in self._landed:
                continue
            if not (task.get("workspace") or {}).get("branch"):
                self._landed.add(task["id"])
                continue
            try:
                ahead = M.unlanded(self.root, self.cfg, task)
            except Exception:
                continue
            if ahead == 0:
                self._landed.add(task["id"])
                continue

            # queued at stage `done` is a state nothing picks up: hand it back
            # to the merge worker rather than leaving it stranded
            B.update(self.db, task["id"], actor="scheduler", status=B.MERGING,
                     defer_until=0,
                     defer_reason=None)
            self.db.emit("merge.unlanded", task["id"], actor="scheduler",
                         commits=ahead,
                         branch=(task.get("workspace") or {}).get("branch"),
                         was=task["status"])
            self.log(f"{task['id']} said done but {ahead} commit(s) never "
                     f"landed — sent back to merge")
    def start_merge(self) -> None:
        """Pick up one card waiting to land. Strictly one at a time."""
        if self._merging or not self.cfg["runner"].get("merge_on_done", True):
            return
        row = self.db.q1(
            "SELECT id FROM tasks WHERE status=? AND (defer_until IS NULL "
            "OR defer_until < ?) ORDER BY updated_at LIMIT 1", (B.MERGING, now()))
        if not row:
            return
        task = B.get(self.db, row["id"])
        if not task:
            return
        self._merging = task["id"]

        def work():
            from dispatch import merge as M
            try:
                outcome, detail = M.merge_card(self.db, self.root, self.cfg,
                                               self.workflows, task)
            except Exception:
                outcome, detail = "conflict", traceback.format_exc()[-3000:]
            self._merge_result = {"task_id": task["id"], "outcome": outcome,
                                  "detail": detail}

        self._merge_thread = threading.Thread(target=work, name="merge", daemon=True)
        self._merge_thread.start()

    def collect_merge(self) -> None:
        res, self._merge_result = self._merge_result, None
        if not res:
            return
        self._merging = None
        self._merge_thread = None
        from dispatch import merge as M

        tid, outcome, detail = res["task_id"], res["outcome"], res["detail"]
        task = B.get(self.db, tid)
        if not task:
            return

        if outcome in (M.MERGED, M.SKIPPED):
            B.mark_merged(self.db, self.cfg, self.workflows, tid, detail)
            self._landed.add(tid)
            if outcome == M.MERGED:
                self.log(f"{tid} merged into "
                         f"{M.base_branch(self.cfg, task, self.root)} ({detail})")
                M.cleanup(self.root, self.cfg, task)
            return

        if outcome == M.BUSY:
            # Nothing is wrong with the card — the repo is not ready. Wait, but
            # not forever and not silently: a dirty base tree blocks *every*
            # card's merge, so an indefinite quiet wait is the worst outcome.
            wait = float(self.cfg["runner"].get("merge_retry_s", 30))
            B.update(self.db, tid, actor="scheduler",
                     defer_until=now() + wait, defer_reason=f"merge: {detail[:200]}")
            self.db.emit("merge.deferred", tid, actor="scheduler", reason=detail[:400])
            self._merge_busy_streak += 1
            limit = int(self.cfg["runner"].get("merge_busy_escalate_after", 10))
            if self._merge_busy_streak >= limit:
                self._escalate_stalled_merges(detail)
            return
        self._merge_busy_streak = 0

        # conflict or a gate that failed on the rebased tree: this is real work
        # for someone, and the card is no longer done.
        B.update(self.db, tid, actor="scheduler",
                 block_reason=f"could not land: {detail.splitlines()[0][:180]}",
                 last_evidence=detail)
        B.open_checkpoint(
            self.db, tid, kind="escalation", topic="merge_conflict", cfg=self.cfg,
            question=f"{tid} passed its pipeline but will not land on "
                     f"{M.base_branch(self.cfg, task, self.root)}",
            bundle={"outcome": outcome, "evidence": detail,
                    "branch": (task.get("workspace") or {}).get("branch"),
                    "note": "The card's own tests passed in isolation. This is "
                            "what happened when it met the base branch."})
        self.db.emit("merge.failed", tid, actor="scheduler", outcome=outcome)
        self.log(f"{tid} failed to land ({outcome})")

    def _escalate_stalled_merges(self, detail: str) -> None:
        """Say it out loud, once, naming the files and every card it holds up."""
        from dispatch.runner import dirty_paths
        already = self.db.q1("SELECT id FROM checkpoints WHERE status='open' "
                             "AND question LIKE 'Merges are stalled%'")
        if already:
            return
        waiting = [dict(r) for r in self.db.q(
            "SELECT id, title FROM tasks WHERE status=?", (B.MERGING,))]
        if not waiting:
            return
        dirty = dirty_paths(self.root)
        B.open_checkpoint(
            self.db, waiting[0]["id"], kind="escalation", topic="merge_stalled",
            cfg=self.cfg,
            question=f"Merges are stalled — {len(waiting)} card(s) cannot land",
            bundle={"reason": detail,
                    "dirty_files": dirty,
                    "waiting": waiting,
                    "note": "Nothing can merge while the base tree has "
                            "uncommitted changes to tracked files. If you did "
                            "not make these edits, an agent wrote outside its "
                            "worktree — check `dispatch log` for "
                            "run.stray_writes. Commit, stash or discard them "
                            "and the queue drains on its own."})
        self.db.emit("merge.stalled", actor="scheduler", waiting=len(waiting),
                     dirty=dirty[:20])
        self.log(f"merges stalled: {len(waiting)} card(s) waiting, "
                 f"{len(dirty)} dirty path(s) in the base tree")
        self._merge_busy_streak = 0

    # -- dispatch -----------------------------------------------------------
    def dispatch(self) -> None:
        from dispatch import sandbox as SB
        if SB.enabled(self.cfg):
            ok, _ = SB.preflight(self.cfg, self.root)
            if not ok:
                if self.ticks % 60 == 1:
                    self.log("dispatch halted: the sandbox is enabled but "
                             "not usable")
                return
        limit = int(self.cfg["scheduler"].get("max_concurrent", 3))
        running = self.db.q1("SELECT COUNT(*) c FROM leases")["c"]
        if running >= limit:
            return

        for task in B.ready_set(self.db, self.cfg, self.workflows):
            if running >= limit:
                return
            entry = W.stage_entry(self.workflows, task["card_type"], task["stage"])
            if entry is None:
                continue

            # A `human` stage is a checkpoint, not a dispatch.
            if entry.get("agent") == "human":
                if self._auto_pass(task, entry):
                    B.advance(self.db, self.cfg, self.workflows, task["id"])
                else:
                    B.open_checkpoint(
                        self.db, task["id"], topic="signoff", cfg=self.cfg,
                        question=f"Sign off on {task['id']} — {task['title']}",
                        bundle=self._bundle(task),
                        sla_s=_sla_seconds(entry.get("sla")))
                continue

            v, _trail = G.evaluate(self._ctx(task), "pre_dispatch")
            if v.verdict == G.DEFER:
                B.update(self.db, task["id"], actor="scheduler",
                         defer_until=now() + (v.retry_after_s or 30),
                         defer_reason=f"{v.gate}: {v.reason}")
                continue
            if v.verdict == G.FAIL:
                self._on_fail(task, v)
                continue
            if v.verdict == G.ESCALATE:
                B.open_checkpoint(self.db, task["id"], kind="escalation",
                                  topic=v.gate, cfg=self.cfg,
                                  question=f"{v.gate} blocked {task['id']}: {v.reason}",
                                  bundle={"gate": v.gate, "reason": v.reason,
                                          "evidence": v.evidence})
                continue

            lock = entry.get("lock")
            if lock and not B.acquire_lock(self.db, lock, task["id"]):
                continue

            self._launch(task, entry)
            running += 1

    def _auto_pass(self, task: dict[str, Any], entry: dict[str, Any]) -> bool:
        """`auto_pass_if` keeps trivia off your plate. Green tests, small diff,
        no new dependencies — don't wake the human."""
        rule = entry.get("auto_pass_if")
        if not rule:
            return False
        ws = task.get("workspace") or {}
        wt, base = ws.get("worktree"), ws.get("base_ref")
        if rule == "small_and_green":
            if not wt or not os.path.isdir(wt):
                return False
            from dispatch.runner import diff_against
            diff, files = diff_against(wt, base or "HEAD")
            added = sum(1 for line in diff.splitlines()
                        if line.startswith("+") and not line.startswith("+++"))
            if added > 20 or len(files) > 3:
                return False
            v, _ = G.evaluate(self._ctx(task, cwd=wt, diff=diff, changed_files=files),
                              "pre_complete")
            passed = v.verdict == G.PASS
            if passed:
                self.db.emit("checkpoint.auto_passed", task["id"], actor="scheduler",
                             rule=rule, added_lines=added, files=len(files))
            return passed
        return False

    def _bundle(self, task: dict[str, Any]) -> dict[str, Any]:
        """A checkpoint carries its own context — the real cost of a human gate
        is the ten minutes spent reconstructing what you are being asked."""
        ws = task.get("workspace") or {}
        wt, base = ws.get("worktree"), ws.get("base_ref")
        diff, files = ("", [])
        if wt and os.path.isdir(wt):
            from dispatch.runner import diff_against
            diff, files = diff_against(wt, base or "HEAD")
        run = self.db.q1("SELECT summary,usd,duration_s,log_dir FROM runs WHERE task_id=? "
                         "ORDER BY started_at DESC LIMIT 1", (task["id"],))
        gr = self.db.q("SELECT gate,verdict,reason FROM gate_runs WHERE task_id=? "
                       "ORDER BY ts DESC LIMIT 12", (task["id"],))
        return {
            "plan": task.get("plan"),
            "summary": run["summary"] if run else None,
            "usd": run["usd"] if run else None,
            "branch": ws.get("branch"),
            "changed_files": files,
            "diff": diff[:200000],
            "gates": [dict(g) for g in gr],
            "acceptance": task.get("acceptance") or [],
            "options": ["approve", "amend", "reject"],
        }

    def _launch(self, task: dict[str, Any], entry: dict[str, Any]) -> None:
        from dispatch.runner import launch
        tid = task["id"]
        lease_s = self.cfg["scheduler"].get("lease_seconds", 3600.0)
        self.db.x("INSERT INTO leases (task_id,run_id,pid,stage,heartbeat_at,expires_at) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                  "heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
                  (tid, "pending", os.getpid(), task["stage"], now(), now() + lease_s))
        B.update(self.db, tid, actor="scheduler", status=B.RUNNING,
                 defer_until=0, defer_reason=None)
        self.log(f"dispatch {tid} [{task['stage']}/{entry.get('agent')}] {task['title'][:60]}")

        def work():
            try:
                res = launch(self.db, self.root, self.cfg, self.workflows,
                             B.get(self.db, tid))
                res["task_id"] = tid
            except Exception:
                res = {"task_id": tid, "exit_code": -1, "error": traceback.format_exc(),
                       "diff": "", "changed_files": [], "cwd": None, "summary": ""}
            with self._results_lock:
                self._results.append(res)

        th = threading.Thread(target=work, name=f"run:{tid}", daemon=True)
        self._threads[tid] = th
        th.start()

    # -- completion ---------------------------------------------------------
    def collect_finished(self) -> None:
        with self._results_lock:
            batch, self._results = self._results, []
        for res in batch:
            try:
                self._finish(res)
            except Exception:
                self.log("finish failed:\n" + traceback.format_exc())

    def _finish(self, res: dict[str, Any]) -> None:
        tid = res["task_id"]
        self.db.x("DELETE FROM leases WHERE task_id=?", (tid,))
        self._threads.pop(tid, None)
        task = B.get(self.db, tid)
        if not task or task["status"] in B.TERMINAL:
            return
        entry = W.stage_entry(self.workflows, task["card_type"], task["stage"]) or {}
        lock = entry.get("lock")

        if res.get("exit_code", 0) != 0 and not res.get("diff"):
            v = G.Verdict(G.FAIL, f"agent exited {res.get('exit_code')}",
                          evidence=(res.get("error") or "")[-4000:] or
                                   "The agent process failed before producing a diff.")
            v.gate = "agent_exit"
            if lock:
                B.release_lock(self.db, lock, tid)
            self._on_fail(task, v)
            return

        ctx = self._ctx(task, cwd=res.get("cwd"), diff=res.get("diff", ""),
                        changed_files=res.get("changed_files", []),
                        diff_file=res.get("diff_file"), summary=res.get("summary", ""),
                        stray_writes=res.get("stray_writes") or [],
                        run_id=res.get("run_id"))
        v, _trail = G.evaluate(ctx, "pre_complete")

        if lock:
            B.release_lock(self.db, lock, tid)

        if v.verdict == G.PASS:
            B.update(self.db, tid, actor="scheduler", last_evidence=None, attempts=0)
            new_stage = B.advance(self.db, self.cfg, self.workflows, tid)
            self.log(f"{tid} cleared {task['stage']} -> {new_stage}")
            if new_stage == B.DONE and self.cfg["runner"].get("worktrees", True):
                pass  # worktree is kept until integrate merges it; see `dispatch gc`
        elif v.verdict == G.DEFER:
            B.update(self.db, tid, actor="scheduler", status=B.QUEUED,
                     defer_until=now() + (v.retry_after_s or 60),
                     defer_reason=f"{v.gate}: {v.reason}")
        elif v.verdict == G.ESCALATE:
            B.open_checkpoint(self.db, tid, kind="escalation",
                              topic=v.gate, cfg=self.cfg,
                              question=f"{v.gate} escalated {tid}: {v.reason}",
                              bundle={**self._bundle(task), "gate": v.gate,
                                      "reason": v.reason, "evidence": v.evidence})
        else:
            self._on_fail(task, v)

    def _on_fail(self, task: dict[str, Any], v) -> None:
        """Send it back with the gate's evidence — that evidence becomes the
        next attempt's instruction. Exhausted attempts quarantine rather than
        retry forever; one poison card shouldn't eat a night's quota."""
        tid = task["id"]
        attempts = task["attempts"] + 1
        evidence = (f"[{v.gate}] {v.reason}\n\n" + (v.evidence or "")).strip()
        if attempts < task["max_attempts"]:
            backoff = float(self.cfg["scheduler"].get("retry_backoff_s", 5))
            B.update(self.db, tid, actor="scheduler", attempts=attempts,
                     status=B.QUEUED, last_evidence=evidence,
                     defer_until=now() + backoff)
            self.db.emit("task.returned", tid, actor="scheduler", gate=v.gate,
                         reason=v.reason, attempt=attempts)
            self.log(f"{tid} returned by {v.gate} ({attempts}/{task['max_attempts']})")
            return

        from dispatch.arbiter import triage_failure
        verdict = triage_failure(self.db, self.cfg, {**task, "attempts": attempts}, evidence)
        action = verdict.get("action", "human")
        if action == "retry" and verdict.get("hint"):
            B.update(self.db, tid, actor="arbiter", attempts=0, status=B.QUEUED,
                     max_attempts=task["max_attempts"] + 1,
                     last_evidence=evidence + "\n\n## Arbiter hint\n" + verdict["hint"])
            self.log(f"{tid} arbiter granted another attempt")
            return
        if action == "decompose" and verdict.get("tasks"):
            P.submit(self.db, from_task=tid, kind="split",
                     payload={"tasks": verdict["tasks"], "parent_id": tid},
                     rationale=verdict.get("reason", "arbiter decomposed a stuck card"))
            B.update(self.db, tid, actor="arbiter", status=B.BLOCKED,
                     block_reason="decomposed into child cards")
            return
        B.update(self.db, tid, actor="scheduler", status=B.DEADLETTER,
                 attempts=attempts, last_evidence=evidence,
                 block_reason=f"{v.gate}: {v.reason}")
        B.open_checkpoint(self.db, tid, kind="escalation",
                          topic="deadletter", cfg=self.cfg,
                          question=f"{tid} exhausted {attempts} attempts — what now?",
                          bundle={**self._bundle(task), "gate": v.gate,
                                  "reason": v.reason, "evidence": evidence})
        self.db.emit("task.deadletter", tid, actor="scheduler", gate=v.gate)
        self.log(f"{tid} dead-lettered after {attempts} attempts")

    # -- proposals ----------------------------------------------------------
    def adjudicate(self) -> None:
        for prop in P.pending(self.db):
            try:
                P.adjudicate(self.db, self.root, self.cfg, self.workflows, prop)
            except Exception:
                self.log("adjudication failed:\n" + traceback.format_exc())
                P._decide(self.db, prop, "escalated", "policy",
                          "adjudicator raised an exception")

    def check_expansion(self) -> None:
        ratio, created, done = P.expansion_ratio(self.db, self.cfg)
        limit = float(self.cfg["containment"].get("expansion_ratio_limit", 2.5))
        if ratio and ratio > limit and not self.cfg["scheduler"].get("paused"):
            already = self.db.q1(
                "SELECT id FROM checkpoints WHERE status='open' AND question LIKE ?",
                ("Expansion alarm%",))
            if already:
                return
            cfg = load_config(self.root)
            cfg["scheduler"]["paused"] = True
            cfg["scheduler"]["paused_reason"] = (
                f"expansion alarm — {created} agent-created card(s) per {done} "
                f"completed, over the {limit} limit")
            from dispatch.config import save_config
            save_config(self.root, cfg)
            root_card = self.db.q1(
                "SELECT id FROM tasks WHERE status NOT IN ('done','cancelled') "
                "ORDER BY created_at LIMIT 1")
            if root_card:
                B.open_checkpoint(
                    self.db, root_card["id"], kind="escalation",
                    topic="expansion", cfg=self.cfg,
                    question=f"Expansion alarm — {created} cards created per {done} completed",
                    bundle={"ratio": round(ratio, 2), "created": created, "done": done,
                            "note": "Dispatch is paused. The board is growing faster "
                                    "than it is shrinking, which usually means the "
                                    "agents are going in circles."})
            self.db.emit("expansion.alarm", actor="scheduler", ratio=ratio,
                         created=created, done=done)
            self.log(f"EXPANSION ALARM ratio={ratio:.2f} — dispatch paused")


# ---------------------------------------------------------------------------
# daemon plumbing
# ---------------------------------------------------------------------------

def write_pid(root: str) -> None:
    with open(paths(root)["pid"], "w") as f:
        f.write(str(os.getpid()))


def read_pid(root: str) -> int | None:
    p = paths(root)["pid"]
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def clear_pid(root: str) -> None:
    try:
        os.remove(paths(root)["pid"])
    except OSError:
        pass

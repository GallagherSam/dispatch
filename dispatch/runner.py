"""Turning a card into a diff.

Context assembly is *derived from the graph*, never remembered: a worker's
prompt is composed from its own brief, artifacts pulled along `artifact` edges,
its parent's brief for framing, and — on a retry — the failing gate's evidence.
Nothing depends on an orchestrator having kept anything in mind.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
import time
from typing import Any

from dispatch.config import load_agents, paths
from dispatch.db import DB, new_id, now

# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _git(root: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, check=check)


def is_git_repo(root: str) -> bool:
    return _git(root, "rev-parse", "--git-dir").returncode == 0


def head_ref(root: str) -> str:
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() or "HEAD"


def make_worktree(root: str, cfg: dict[str, Any], task_id: str,
                  stage: str) -> tuple[str | None, str | None, str | None]:
    """Returns (worktree_path, branch, base_ref).  Falls back to working in the
    repo itself when worktrees are disabled or unavailable."""
    if not cfg["runner"].get("worktrees", True) or not is_git_repo(root):
        return None, None, None
    p = paths(root)
    os.makedirs(p["worktrees"], exist_ok=True)
    wt = os.path.join(p["worktrees"], task_id)
    branch = cfg["runner"].get("branch_prefix", "dispatch/") + task_id
    base = head_ref(root)

    existing = _git(root, "worktree", "list", "--porcelain").stdout
    if wt in existing and os.path.isdir(wt):
        return wt, branch, base

    r = _git(root, "worktree", "add", "-b", branch, wt)
    if r.returncode != 0:
        # branch already exists (a retry of an earlier stage) — reuse it
        r2 = _git(root, "worktree", "add", wt, branch)
        if r2.returncode != 0:
            return None, None, None
    _write_excludes(wt, cfg)
    return wt, branch, base


# Gates run test suites inside the worktree, and test runners leave litter.
# Without this it lands in the commit, inflates the diff, and trips `diff_scope`
# and the `small_and_green` auto-pass rule on work that was actually clean.
RESULT_FILENAME = ".dispatch-result.json"

DEFAULT_EXCLUDES = [
    RESULT_FILENAME,
    "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    ".tox/", ".coverage", "coverage.xml", "htmlcov/", ".DS_Store",
    "node_modules/", ".next/", "dist/", "build/", "*.egg-info/",
    "target/", ".gradle/", ".venv/", "venv/",
]


def _write_excludes(worktree: str, cfg: dict[str, Any]) -> None:
    """Per-worktree ignores via `.git/info/exclude` — never touches the user's
    tracked .gitignore."""
    path = _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    if not path:
        return
    if not os.path.isabs(path):
        path = os.path.join(worktree, path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        patterns = list(DEFAULT_EXCLUDES) + list(cfg.get("runner", {}).get("exclude", []))
        with open(path, "a") as f:
            f.write("\n# added by dispatch — build and test litter\n")
            f.write("\n".join(patterns) + "\n")
    except OSError:
        pass


def remove_worktree(root: str, task_id: str, keep_branch: bool = True) -> None:
    p = paths(root)
    wt = os.path.join(p["worktrees"], task_id)
    if os.path.isdir(wt):
        _git(root, "worktree", "remove", "--force", wt)
    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)
    _git(root, "worktree", "prune")


def dirty_paths(root: str) -> list[str]:
    """Tracked-and-modified plus untracked paths in the main repo, ignoring
    dispatch's own directory."""
    if not is_git_repo(root):
        return []
    out = _git(root, "status", "--porcelain").stdout.splitlines()
    paths_ = []
    for line in out:
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if path.startswith(".dispatch/") or path == ".dispatch":
            continue
        paths_.append(path)
    return sorted(paths_)


def commit_all(worktree: str, message: str) -> str | None:
    """Agents are told not to commit; the runner does it so every stage boundary
    is a real commit and the diff is always computable."""
    _git(worktree, "add", "-A")
    st = _git(worktree, "status", "--porcelain")
    if not st.stdout.strip():
        return None
    _git(worktree, "commit", "-m", message, "--no-verify")
    return _git(worktree, "rev-parse", "HEAD").stdout.strip() or None


def diff_against(worktree: str, base_ref: str) -> tuple[str, list[str]]:
    """Returns (unified diff, changed file paths) including uncommitted work."""
    if not worktree or not is_git_repo(worktree):
        return "", []
    merge_base = _git(worktree, "merge-base", "HEAD", base_ref).stdout.strip() or base_ref
    _git(worktree, "add", "-A")
    d = _git(worktree, "diff", merge_base, "--").stdout
    files = _git(worktree, "diff", "--name-only", merge_base, "--").stdout.split()
    return d, [f for f in files if f]


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------

def _artifact_context(db: DB, task_id: str) -> str:
    """`artifact` edges declare "B consumes A's output".  Pull that in
    automatically so handoff is declarative, not remembered."""
    from dispatch import board as B
    srcs = [r["src"] for r in
            db.q("SELECT src FROM edges WHERE dst=? AND kind='artifact'", (task_id,))]
    if not srcs:
        return ""
    chunks = []
    for s in srcs:
        t = B.get(db, s)
        if not t:
            continue
        arts = t.get("artifacts") or []
        summary = db.q1(
            "SELECT summary FROM runs WHERE task_id=? AND summary IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1", (s,))
        block = [f"### From {s} — {t['title']}"]
        if summary and summary["summary"]:
            block.append(summary["summary"].strip()[:2500])
        for a in arts[:12]:
            block.append(f"- artifact: {a}")
        chunks.append("\n".join(block))
    return "\n\n".join(chunks)


def build_prompt(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
                 task: dict[str, Any], run_id: str, result_path: str) -> str:
    from dispatch import board as B
    from dispatch.workflows import next_stage, stage_entry

    entry = stage_entry(workflows, task["card_type"], task["stage"]) or {}
    nxt = next_stage(workflows, task["card_type"], task["stage"])
    parent = B.get(db, task["parent_id"]) if task.get("parent_id") else None

    parts: list[str] = []
    parts.append(f"# Task {task['id']} — {task['title']}")
    parts.append(
        f"**Stage:** `{task['stage']}` · **Your role:** `{entry.get('agent', task.get('agent_type'))}` · "
        f"**Card type:** `{task['card_type']}` · "
        f"**Attempt:** {task['attempts'] + 1} of {task['max_attempts']}")
    if nxt:
        parts.append(f"When you finish, this card moves to `{nxt['stage']}` "
                     f"(worked by `{nxt.get('agent')}`). Leave it ready for them.")
    else:
        parts.append("This is the final stage of the pipeline for this card type.")

    if parent:
        parts.append("## Parent card (framing, not your job)\n"
                     f"**{parent['title']}**\n\n{(parent['brief'] or '')[:1200]}")

    parts.append("## Brief\n" + (task["brief"] or "(no brief — infer from the title)"))

    if task.get("acceptance"):
        lines = "\n".join(f"- {a}" for a in task["acceptance"])
        parts.append("## Acceptance criteria\nYou are judged against exactly these:\n" + lines)

    scope = (task.get("workspace") or {}).get("scope") or []
    if scope:
        parts.append("## Scope (enforced)\nYou may only modify files matching:\n" +
                     "\n".join(f"- `{g}`" for g in scope) +
                     "\n\nA diff touching anything else is rejected by a gate.")

    art = _artifact_context(db, task["id"])
    if art:
        parts.append("## Inputs from upstream cards\n" + art)

    from dispatch import memory as MEM
    known = MEM.brief_for(db, task)
    if known:
        parts.append("## What earlier agents learned about this repo\n"
                     "Treat these as starting points, not gospel — verify "
                     "before relying on one, and correct it if it is wrong.\n\n"
                     + known)
    parts.append("## Shared memory\n" + MEM.usage_note(db.get_meta("board_url")))

    if task.get("last_evidence"):
        parts.append("## Why the previous attempt was returned\n"
                     "Fix this specifically.\n\n```\n" +
                     str(task["last_evidence"])[:6000] + "\n```")

    gates = [g if isinstance(g, str) else g.get("gate") for g in entry.get("gates", [])]
    if gates:
        parts.append("## Gates that will judge your work\n" +
                     "\n".join(f"- `{g}`" for g in gates))

    parts.append(textwrap.dedent(f"""
        ## How to finish

        Do the work in this directory. **Do not commit** — the orchestrator commits
        for you at the stage boundary.

        If you discover work that belongs on the board but not in this card, propose it
        rather than doing it:

        ```
        dispatch propose --from {task['id']} --kind add_task \\
          --title "..." --brief "..." \\
          --accept "a runnable command that proves it is done" \\
          --scope "src/thing/**" \\
          --rationale "why this is out of scope here"
        ```

        `--accept` is required and it must be checkable. A card with nothing to
        check is refused before it costs a run, so proposing one only spends a
        human's attention.

        Valid kinds: `add_task`, `split`, `add_dep`, `amend_brief`, `raise_blocker`,
        `request_gate`, `escalate`. Proposals are adjudicated — you are not writing to
        the board directly, and you cannot unblock yourself.

        When you are done, write your result to `{result_path}`:

        ```json
        {{"summary": "what you did, in a few sentences",
          "artifacts": ["path/or/url", "..."],
          "confidence": 0.0}}
        ```

        Then stop. Do not mark the card done — only a gate may do that.
    """).strip())

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# invocation
# ---------------------------------------------------------------------------

def _render(cmd_template: list[str], subs: dict[str, str]) -> list[str]:
    out = []
    for part in cmd_template:
        rendered = part
        for k, v in subs.items():
            rendered = rendered.replace("{" + k + "}", str(v))
        if rendered.strip() == "" and any("{" + k + "}" in part for k in subs):
            continue  # a substitution resolved to nothing — drop the flag value
        out.append(rendered)
    return out


DEFAULT_MODEL = "sonnet"


def resolve_model(workflows: dict[str, Any], agents: dict[str, Any],
                  task: dict[str, Any]) -> str:
    """Card beats stage beats agent role.

    The card is most specific — "this one needs Opus" — and the stage is where
    you say "review is always worth the better model" once instead of on every
    card. The agent role is the floor.
    """
    from dispatch.workflows import stage_entry

    if task.get("model"):
        return str(task["model"])
    entry = stage_entry(workflows, task.get("card_type"), task.get("stage")) or {}
    if entry.get("model"):
        return str(entry["model"])
    spec = agents.get(task.get("agent_type") or "", {})
    return str(spec.get("model") or DEFAULT_MODEL)


def agent_prompt_file(root: str, agent_type: str, agents: dict[str, Any]) -> str:
    spec = agents.get(agent_type) or {}
    fn = spec.get("prompt_file")
    if not fn:
        return ""
    p = os.path.join(paths(root)["agents"], fn)
    return p if os.path.exists(p) else ""


def launch(db: DB, root: str, cfg: dict[str, Any], workflows: dict[str, Any],
           task: dict[str, Any]) -> dict[str, Any]:
    """Run one stage of one card. Blocking — the scheduler calls this in a thread."""
    from dispatch import board as B

    agents = load_agents(root)
    agent_type = task.get("agent_type") or "developer"
    run_id = new_id("r")
    p = paths(root)
    log_dir = os.path.join(p["runs"], run_id)
    os.makedirs(log_dir, exist_ok=True)

    wt, branch, base = make_worktree(root, cfg, task["id"], task["stage"])
    cwd = wt or root
    base_ref = base or head_ref(root)
    ws = dict(task.get("workspace") or {})
    ws.update({"worktree": wt, "branch": branch, "base_ref": base_ref})
    # Anything already dirty in the worktree before the agent starts — engine
    # sidecars, generated files — is not this card's doing, and blaming the
    # agent for it is how a gate loses its credibility.
    if wt:
        ws["pre_existing"] = dirty_paths(wt)
    B.update(db, task["id"], actor="scheduler", workspace=ws)
    task = B.get(db, task["id"])

    # Inside the worktree: an agent is sandboxed to its own directory, so a
    # result path under .dispatch/runs/ is unreachable to it.
    result_path = os.path.join(cwd, RESULT_FILENAME)
    prompt = build_prompt(db, root, cfg, workflows, task, run_id, result_path)
    with open(os.path.join(log_dir, "prompt.md"), "w") as f:
        f.write(prompt)

    spec = agents.get(agent_type, {})
    model = resolve_model(workflows, agents, task)
    subs = {
        "agent_prompt_file": agent_prompt_file(root, agent_type, agents),
        "allowed_tools": spec.get("allowed_tools", "Read,Write,Edit,Grep,Glob,Bash"),
        "model": model,
        "task_id": task["id"],
        "stage": task["stage"],
        "worktree": cwd,
        "settings_file": p["settings"] if os.path.exists(p["settings"]) else "",
        "permission_mode": cfg["runner"].get("permission_mode", "acceptEdits"),
    }
    cmd = _render(list(cfg["runner"]["command"]), subs)
    # Drop a flag whose value failed to resolve (e.g. no agent prompt file).
    cleaned: list[str] = []
    skip_next = False
    for i, part in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if part == "--settings" and not subs["settings_file"]:
            skip_next = True
            continue
        if part in ("--append-system-prompt-file", "--model", "--allowedTools",
                    "--settings") and (
                i + 1 >= len(cmd) or not cmd[i + 1] or cmd[i + 1].startswith("--")):
            skip_next = False
            continue
        if part == "--append-system-prompt-file" and not subs["agent_prompt_file"]:
            skip_next = True
            continue
        cleaned.append(part)
    cmd = cleaned

    # Confine the agent to its own worktree at the OS level. Writes elsewhere
    # fail with EPERM rather than being caught later by a gate.
    from dispatch import sandbox as SB
    sandbox_meta: dict[str, Any] = {}
    if SB.enabled(cfg):
        cmd, sandbox_meta = SB.wrap(cfg, cmd, cwd, log_dir)

    db.x("INSERT INTO runs (id,task_id,stage,agent_type,model,attempt,status,"
         "log_dir,started_at) VALUES (?,?,?,?,?,?,?,?,?)",
         (run_id, task["id"], task["stage"], agent_type, model,
          task["attempts"] + 1, "running", log_dir, now()))
    db.emit("run.started", task["id"], run_id=run_id, stage=task["stage"],
            agent=agent_type, model=model, sandboxed=bool(sandbox_meta),
            sandbox=sandbox_meta.get("backend"),
            cmd=" ".join(shlex.quote(c) for c in cmd))

    from dispatch.config import agent_environment
    env = agent_environment(cfg)
    env.update({
        "DISPATCH_ROOT": root,
        "DISPATCH_TASK_ID": task["id"],
        "DISPATCH_RUN_ID": run_id,
        "DISPATCH_STAGE": task["stage"],
        "DISPATCH_RESULT": result_path,
        "DISPATCH_BOARD_DB": p["db"],
    })

    # An agent can resolve an absolute path and write into the main repo
    # instead of its worktree. That work never reaches the card's branch, and
    # it dirties the base tree — which stalls *every* card's merge. Snapshot
    # what was already dirty so anything new can be attributed.
    dirty_before = set(dirty_paths(root))

    started = time.time()
    stdout_path = os.path.join(log_dir, "stdout.json")
    stderr_path = os.path.join(log_dir, "stderr.txt")
    exit_code, raw = -1, ""
    try:
        with open(stdout_path, "w") as so, open(stderr_path, "w") as se:
            proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, text=True,
                                  stdout=so, stderr=se,
                                  timeout=cfg["runner"].get("timeout_s", 3600))
            exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        exit_code = -9
        with open(stderr_path, "a") as se:
            se.write("\n[dispatch] agent exceeded timeout_s\n")
    except FileNotFoundError as e:
        exit_code = -127
        with open(stderr_path, "a") as se:
            se.write(f"\n[dispatch] cannot launch agent: {e}\n")

    duration = time.time() - started
    try:
        with open(stdout_path) as f:
            raw = f.read()
    except OSError:
        raw = ""

    summary, usd = _parse_agent_output(raw)
    result = _read_result(result_path)
    if result:
        # Keep a copy with the run, then take it out of the tree so it never
        # reaches a commit or a diff.
        try:
            shutil.copyfile(result_path, os.path.join(log_dir, "result.json"))
        except OSError:
            pass
    try:
        os.remove(result_path)
    except OSError:
        pass
    if result.get("summary"):
        summary = result["summary"]

    commit = commit_all(cwd, f"dispatch({task['stage']}): {task['title']} [{task['id']}]") \
        if wt else None
    diff, changed = diff_against(cwd, base_ref) if wt else ("", [])
    diff_file = os.path.join(log_dir, "diff.patch")
    with open(diff_file, "w") as f:
        f.write(diff)

    db.x("UPDATE runs SET status=?, exit_code=?, summary=?, usd=?, duration_s=?, "
         "finished_at=? WHERE id=?",
         ("finished" if exit_code == 0 else "errored", exit_code, summary, usd,
          duration, now(), run_id))
    db.emit("run.finished", task["id"], run_id=run_id, exit_code=exit_code,
            usd=usd, duration_s=round(duration, 1), files=len(changed))

    if result.get("plan"):
        B.update(db, task["id"], actor="agent",
                 plan=json.dumps(result["plan"], default=str))
    if result.get("artifacts"):
        arts = list(task.get("artifacts") or []) + list(result["artifacts"])
        B.update(db, task["id"], actor="agent", artifacts=arts[:60])

    for prop in result.get("proposals", []) or []:
        from dispatch import proposals as P
        try:
            P.submit(db, from_task=task["id"], kind=prop.get("kind", "add_task"),
                     payload=prop.get("payload", prop), rationale=prop.get("rationale", ""),
                     confidence=prop.get("confidence"), urgency=prop.get("urgency", "normal"))
        except Exception:
            pass

    stray = sorted(set(dirty_paths(root)) - dirty_before) if wt else []
    if stray:
        db.emit("run.stray_writes", task["id"], run_id=run_id, paths=stray[:20])

    return {
        "run_id": run_id, "exit_code": exit_code, "summary": summary, "usd": usd,
        "duration_s": duration, "diff": diff, "diff_file": diff_file,
        "changed_files": changed, "cwd": cwd, "log_dir": log_dir, "commit": commit,
        "stray_writes": stray,
    }


def _parse_agent_output(raw: str) -> tuple[str, float | None]:
    """`claude -p --output-format json` emits one object; be liberal anyway."""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    try:
        d = json.loads(raw)
        if isinstance(d, list):
            d = d[-1] if d else {}
        return (str(d.get("result") or d.get("text") or "")[:8000],
                d.get("total_cost_usd") or d.get("cost_usd"))
    except json.JSONDecodeError:
        pass
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    if isinstance(last, dict):
        return (str(last.get("result") or "")[:8000],
                last.get("total_cost_usd") or last.get("cost_usd"))
    return raw[-8000:], None


def _read_result(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

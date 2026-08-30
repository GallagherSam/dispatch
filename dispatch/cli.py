"""Command surface.

`dispatch init` scaffolds `.dispatch/` inside an existing repo; every other
command operates on the board it finds by walking up from the cwd.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any

from dispatch import DISPATCH_DIR, __version__
from dispatch import board as B
from dispatch import proposals as P
from dispatch import workflows as W
from dispatch.config import (
    DEFAULT_AGENTS,
    DEFAULT_CONFIG,
    find_root,
    load_agents,
    load_config,
    paths,
    save_agents,
    save_config,
)
from dispatch.db import DB, open_db

SCAFFOLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scaffold")

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
     "r": "\033[31m", "c": "\033[36m", "0": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = dict.fromkeys(C, "")


def _p(s: str = "") -> None:
    print(s)


def _clip(text: str, limit: int, remedy: str) -> str:
    """Clip loudly, and name a remedy that exists on the command doing the
    clipping.

    Silent truncation once cost someone ten minutes verifying an --append-brief
    that had landed. Naming a flag the command does not have cost the next
    person more: they tried it, got nothing, and assumed they had typed it
    wrong.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… clipped {len(text) - limit} more characters — {remedy}"


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    root = os.path.abspath(args.path or os.getcwd())
    p = paths(root)

    settings_flags = (args.test_cmd, args.lint_cmd, args.build_cmd,
                      args.auth, args.sandbox, args.no_sandbox,
                      args.sandbox_backend)
    if os.path.isdir(p["root"]) and not args.force:
        if any(settings_flags):
            return _update_settings(root, args)
        _p(f"{C['y']}already initialised{C['0']} — {p['root']}")
        _p("pass --test-cmd/--lint-cmd/--build-cmd/--auth/--sandbox to change "
           "just those, or --force to reset config, workflows and agent "
           "prompts (the board itself is left alone)")
        return 1

    is_repo = subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"],
                             capture_output=True).returncode == 0
    if not is_repo:
        if args.git_init:
            subprocess.run(["git", "-C", root, "init", "-q"], check=False)
            is_repo = True
        else:
            _p(f"{C['y']}note{C['0']} — {root} is not a git repo. Worktree isolation "
               "and diff gates need one.")
            _p("      run `dispatch init --git-init` to create one, or continue "
               "without isolation.")

    for d in ("root", "gates", "agents", "runs", "worktrees"):
        os.makedirs(p[d], exist_ok=True)

    for src_dir, dst_dir in ((os.path.join(SCAFFOLD, "gates"), p["gates"]),
                             (os.path.join(SCAFFOLD, "agents"), p["agents"])):
        if not os.path.isdir(src_dir):
            continue
        for fn in os.listdir(src_dir):
            dst = os.path.join(dst_dir, fn)
            if os.path.exists(dst) and not args.force:
                continue
            shutil.copy2(os.path.join(src_dir, fn), dst)
            if fn.endswith((".sh", ".py")):
                os.chmod(dst, 0o755)

    cfg = load_config(root) if os.path.exists(p["config"]) and not args.force \
        else json.loads(json.dumps(DEFAULT_CONFIG))
    if args.auth:
        cfg.setdefault("runner", {})["auth"] = args.auth
    if args.no_sandbox:
        cfg.setdefault("sandbox", {})["enabled"] = False
    elif args.sandbox or args.sandbox_backend:
        cfg.setdefault("sandbox", {})["enabled"] = True
        if args.sandbox_backend:
            cfg["sandbox"]["backend"] = args.sandbox_backend
    cfg.setdefault("commands", {})
    cfg["commands"].setdefault("timeout_s", 900)
    for key, flag in (("test", args.test_cmd), ("lint", args.lint_cmd),
                      ("build", args.build_cmd)):
        if flag:
            cfg["commands"][key] = flag
    test_note = ""
    if not cfg["commands"].get("test"):
        guess = _guess_test_command(root)
        cfg["commands"]["test"], test_note = _settle_test_command(
            root, guess, explicit=False, verify=not args.no_verify)
    elif args.test_cmd:
        cfg["commands"]["test"], test_note = _settle_test_command(
            root, args.test_cmd, explicit=True, verify=not args.no_verify)
    save_config(root, cfg)

    if not os.path.exists(p["agents_json"]) or args.force:
        save_agents(root, json.loads(json.dumps(DEFAULT_AGENTS)))

    if not os.path.exists(p["settings"]) or args.force:
        _write_agent_settings(root, cfg)

    db = DB(p["db"])
    if not W.load(db) or args.force:
        W.save(db, json.loads(json.dumps(W.DEFAULT_WORKFLOWS)), actor="init")
    W.export_file(root, db)
    db.set_meta("initialised_at", str(time.time()))
    db.set_meta("version", __version__)
    db.emit("board.initialised", actor="human", root=root)

    _add_gitignore(root)

    _p(f"{C['g']}board ready{C['0']} — {p['root']}")
    _p()
    _p(f"  {C['dim']}board.db{C['0']}        sqlite: cards, edges, gates, events")
    _p(f"  {C['dim']}config.json{C['0']}     stages, concurrency, containment, runner")
    _p(f"  {C['dim']}workflows.json{C['0']}  card types → pipelines (commit this; "
       f"import it into the next repo)")
    _p(f"  {C['dim']}agents/{C['0']}         one prompt file per agent role")
    _p(f"  {C['dim']}gates/{C['0']}          executable gates — stdin is the card, "
       f"stdout is a verdict")
    _p()
    from dispatch import net as N
    from dispatch import sandbox as SB
    shown = cfg["commands"].get("test") or "(none — set one with --test-cmd)"
    _p(f"test command: {C['c']}{shown}{C['0']}")
    if test_note:
        _p(f"              {test_note}")
    _p(f"board port:   {C['c']}{N.resolve_port(cfg, root)}{C['0']} "
       f"{C['dim']}(stable for this repo, so boards don't collide){C['0']}")
    from dispatch.config import auth_note
    _p(f"agents bill:  {C['c']}{auth_note(cfg)}{C['0']}")
    ok, problems = SB.preflight(cfg, root)
    if SB.enabled(cfg) and ok:
        _p(f"sandbox:      {C['g']}on{C['0']} — {SB.describe(cfg)}")
    for pr in problems:
        _p(f"sandbox:      {C['y']}{pr}{C['0']}")
    _p(f"{C['dim']}agents may run it themselves — permissions in settings.json. "
       f"Set runner.permission_mode to \"bypassPermissions\" to skip that list "
       f"entirely (they are confined to a throwaway worktree either way).{C['0']}")
    _p()
    _p("next:")
    _p(f"  {C['b']}dispatch docs setup{C['0']}   # how to stand this up, in a page")
    _p(f"  {C['b']}dispatch add{C['0']} \"first task\" --brief \"...\" --accept \"tests pass\" --start")
    _p(f"  {C['b']}dispatch up{C['0']}          # scheduler + board at http://127.0.0.1:"
       f"{cfg['server']['port']}")
    return 0


# Read-only or repo-local commands an agent needs to check its own work.
_BASE_ALLOW = [
    "Read", "Write", "Edit", "Grep", "Glob",
    # agents are expected to research; without these the tools are denied and
    # the agent falls back on stale memory without saying so
    "WebSearch", "WebFetch",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git ls-files:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
    "Bash(tail:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(grep:*)", "Bash(rg:*)",
    "Bash(sed:*)", "Bash(awk:*)", "Bash(mkdir:*)", "Bash(echo:*)", "Bash(which:*)",
]
# The command that runs a test suite is not knowable in advance, so init
# derives entries from whatever it detected or you passed in.
_ALLOW_HINTS = [
    "Bash(npm test:*)", "Bash(npm run:*)", "Bash(npx:*)", "Bash(node:*)",
    "Bash(pnpm:*)", "Bash(yarn:*)",
    "Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python -m pytest:*)",
    "Bash(python3:*)", "Bash(ruff:*)", "Bash(mypy:*)",
    "Bash(go test:*)", "Bash(go build:*)", "Bash(go vet:*)",
    "Bash(cargo test:*)", "Bash(cargo build:*)", "Bash(cargo clippy:*)",
    "Bash(make:*)",
]
_DENY = [
    "Bash(git push:*)", "Bash(git commit:*)", "Bash(gh:*)", "Bash(curl:*)",
    "Bash(wget:*)", "Bash(ssh:*)", "Bash(sudo:*)", "Bash(rm -rf /:*)",
    "Read(./.env)", "Read(./.env.*)", "Read(**/.aws/**)", "Read(**/.ssh/**)",
]


def _write_agent_settings(root: str, cfg: dict[str, Any]) -> None:
    allow = list(_BASE_ALLOW)
    for key in ("test", "lint", "build"):
        cmd = (cfg.get("commands") or {}).get(key)
        if not cmd:
            continue
        head = " ".join(str(cmd).split()[:2]).rstrip(":")
        entry = f"Bash({head}:*)"
        if entry not in allow:
            allow.append(entry)
    for h in _ALLOW_HINTS:
        if h not in allow:
            allow.append(h)
    settings = {
        "_comment": "Permissions granted to dispatch agents inside their worktree. "
                    "Agents must be able to run the project's own tests, or they "
                    "write blind. Add commands here rather than loosening "
                    "runner.permission_mode.",
        "permissions": {"allow": allow, "deny": _DENY},
    }
    with open(paths(root)["settings"], "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _verify_command(root: str, cmd: str, timeout: float = 90.0):
    """Actually run it. A test command that does not work makes every
    completion gate meaningless while the board looks healthy — which is worse
    than having none, because nothing tells you."""
    head = (cmd or "").split()
    if not head:
        return False, "empty"
    if shutil.which(head[0]) is None:
        return False, f"`{head[0]}` is not on PATH"
    try:
        out = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                             text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # a wrong command fails in seconds; a slow one is probably a real suite
        return None, f"still running after {int(timeout)}s"
    except OSError as e:
        return False, str(e)
    if out.returncode != 0:
        tail = (out.stdout + out.stderr).strip().splitlines()
        hint = tail[-1][:110] if tail else ""
        return False, f"exited {out.returncode}" + (f" — {hint}" if hint else "")
    return True, "verified"


def _settle_test_command(root: str, cmd: str, explicit: bool, verify: bool):
    """Returns (command_to_store, note). A guess that does not run is not
    stored: refusing to guess beats guessing wrong and looking healthy."""
    if not cmd:
        return "", (f"{C['y']}nothing detected — set one with "
                    f"`dispatch init --test-cmd \"...\"`{C['0']}")
    if not verify:
        return cmd, f"{C['dim']}not verified (--no-verify){C['0']}"
    ok, detail = _verify_command(root, cmd)
    if ok:
        return cmd, f"{C['g']}verified — it runs and passes here{C['0']}"
    if ok is None:
        return cmd, (f"{C['dim']}{detail}, so it was stored unverified — "
                     f"a wrong command fails fast, a real suite is just slow"
                     f"{C['0']}")
    if explicit:
        return cmd, (f"{C['y']}warning: {detail}. Stored anyway because you "
                     f"asked for it — but tests_pass will fail every card "
                     f"until it works.{C['0']}")
    return "", (f"{C['y']}detected `{cmd}` but {detail}, so it was NOT stored — "
                f"guessing wrong here silently breaks every gate.{C['0']}\n"
                f"              set the real one with "
                f"`dispatch init --test-cmd \"...\"`")


def _update_settings(root: str, args) -> int:
    """Change individual settings on a board that already exists."""
    cfg = load_config(root)
    cfg.setdefault("commands", {})
    changed = []
    for key, flag in (("lint", args.lint_cmd), ("build", args.build_cmd)):
        if flag:
            cfg["commands"][key] = flag
            changed.append(f"commands.{key} = {flag}")
    if args.test_cmd:
        stored, note = _settle_test_command(root, args.test_cmd, explicit=True,
                                            verify=not args.no_verify)
        cfg["commands"]["test"] = stored
        changed.append(f"commands.test = {stored}")
        _p(note)
    if args.auth:
        cfg.setdefault("runner", {})["auth"] = args.auth
        changed.append(f"runner.auth = {args.auth}")
    if args.no_sandbox:
        cfg.setdefault("sandbox", {})["enabled"] = False
        changed.append("sandbox.enabled = false")
    elif args.sandbox or args.sandbox_backend:
        cfg.setdefault("sandbox", {})["enabled"] = True
        changed.append("sandbox.enabled = true")
        if args.sandbox_backend:
            cfg["sandbox"]["backend"] = args.sandbox_backend
            changed.append(f"sandbox.backend = {args.sandbox_backend}")
    if not changed:
        _p("nothing to change")
        return 1
    save_config(root, cfg)
    _write_agent_settings(root, cfg)
    _p(f"{C['g']}updated{C['0']} {paths(root)['config']}")
    for c in changed:
        _p(f"  {c}")
    return 0


def _guess_test_command(root: str) -> str:
    j = os.path.join(root, "package.json")
    if os.path.exists(j):
        try:
            with open(j) as f:
                pkg = json.load(f)
            if "test" in (pkg.get("scripts") or {}):
                return "npm test"
        except Exception:
            pass
    if os.path.exists(os.path.join(root, "pyproject.toml")) or \
       os.path.isdir(os.path.join(root, "tests")):
        return "pytest -q"
    if os.path.exists(os.path.join(root, "go.mod")):
        return "go test ./..."
    if os.path.exists(os.path.join(root, "Cargo.toml")):
        return "cargo test"
    if os.path.exists(os.path.join(root, "Makefile")):
        return "make test"
    return ""


def _add_gitignore(root: str) -> None:
    """Keep the board's state out of the repo's history, but keep the parts
    that are configuration."""
    gi = os.path.join(root, DISPATCH_DIR, ".gitignore")
    if os.path.exists(gi):
        return
    with open(gi, "w") as f:
        f.write(textwrap.dedent("""\
            # Board state is local; configuration is shared.
            board.db
            board.db-wal
            board.db-shm
            runs/
            worktrees/
            scheduler.pid
            scheduler.log
            # config that IS worth committing: config.json, workflows.json,
            # settings.json, agents.json, agents/, gates/
            """))


# ---------------------------------------------------------------------------
# card commands
# ---------------------------------------------------------------------------

def _ctx(args):
    root = find_root(getattr(args, "root", None))
    db = open_db(root)
    return root, db, load_config(root), W.load(db)


def cmd_add(args) -> int:
    _root, db, cfg, wfs = _ctx(args)
    if args.card_type not in wfs:
        _p(f"{C['r']}unknown card type '{args.card_type}'{C['0']} — "
           f"have: {', '.join(wfs)}")
        return 1
    tid = B.create(db, cfg, wfs, title=args.title, brief=args.brief or "",
                   card_type=args.card_type, acceptance=args.accept or [],
                   parent_id=args.parent, tags=args.tag or [],
                   priority=args.priority, scope=args.scope or [],
                   model=args.model,
                   depends_on=args.depends_on or [],
                   budget={"usd": args.budget} if args.budget else None,
                   max_attempts=args.max_attempts)
    if args.start:
        B.start_card(db, wfs, tid)
    _p(f"{C['g']}{tid}{C['0']}  {args.title}")
    if args.start:
        pipe = " → ".join(e["stage"] for e in W.pipeline(wfs, args.card_type))
        _p(f"{C['dim']}started · pipeline: {pipe}{C['0']}")
    if not args.accept:
        _p(f"{C['y']}no acceptance criteria{C['0']} — a gate can only be as good as "
           f"the check behind it. Add one with --accept.")
    return 0


def cmd_ls(args) -> int:
    _root, db, cfg, wfs = _ctx(args)
    tasks = B.all_tasks(db, include_terminal=args.all)
    if args.stage:
        tasks = [t for t in tasks if t["stage"] == args.stage]
    if args.card_type:
        tasks = [t for t in tasks if t["card_type"] == args.card_type]
    if not tasks:
        _p(f"{C['dim']}no cards{C['0']}")
        return 0
    leased = {r["task_id"] for r in db.q("SELECT task_id FROM leases")}
    if args.json:
        _p(json.dumps([{
            "id": t["id"], "title": t["title"], "card_type": t["card_type"],
            "stage": t["stage"], "status": t["status"],
            "running": t["id"] in leased, "priority": t["priority"],
            "model": t.get("model"),
            "attempts": t["attempts"], "max_attempts": t["max_attempts"],
            "tags": t["tags"], "parent_id": t["parent_id"],
            "blocked_by": B.blockers(db, cfg, wfs, t),
        } for t in tasks], indent=2))
        return 0
    for t in tasks:
        colour = {"done": C["dim"], "deadletter": C["r"], "failed": C["r"],
                  "checkpoint": C["y"], "blocked": C["y"]}.get(t["status"], "")
        # the id is always the first field: a marker in front of it shifts
        # every column and breaks anything parsing this
        status = t["status"] + ("*" if t["id"] in leased else "")
        scolour = C["g"] if t["id"] in leased else colour
        _p(f"{colour}{t['id']}{C['0']}  {t['stage']:<10} "
           f"{scolour}{status:<12}{C['0']} "
           f"{C['dim']}{t['card_type']:<12}{C['0']} {t['title'][:58]}")
    if any(t["id"] in leased for t in tasks):
        _p(f"{C['dim']}* an agent is running on it{C['0']}")
    # A card sitting in `merging` looks identical whether it is about to land
    # or has been stuck for an hour on something only it knows about. The
    # reason was reachable through `blocked` and nowhere an operator actually
    # looks, which made "why has nothing moved" the most expensive question on
    # the board.
    stuck = [t for t in tasks
             if t["status"] == B.MERGING and (t.get("defer_reason") or "")]
    if stuck:
        _p()
        for t in stuck:
            why = t["defer_reason"]
            why = why[len("merge: "):] if why.startswith("merge: ") else why
            _p(f"{C['y']}{t['id']} cannot land{C['0']} — {why.splitlines()[0][:90]}")
    return 0


def cmd_show(args) -> int:
    root, db, cfg, wfs = _ctx(args)
    t = B.get(db, args.id)
    if not t:
        _p(f"{C['r']}no such card{C['0']}")
        return 1
    _p(f"{C['b']}{t['id']}{C['0']}  {t['title']}")
    from dispatch.config import load_agents
    from dispatch.runner import resolve_model
    model = resolve_model(wfs, load_agents(root), t)
    origin = ("this card" if t.get("model")
              else "the stage" if (W.stage_entry(wfs, t["card_type"], t["stage"]) or {}).get("model")
              else f"the {t['agent_type']} role")
    _p(f"{C['dim']}{t['card_type']} · {t['stage']} / {t['status']} · "
       f"agent={t['agent_type']} · model={model} (from {origin}) · "
       f"attempts {t['attempts']}/{t['max_attempts']}{C['0']}")
    pipe = W.pipeline(wfs, t["card_type"])
    _p("pipeline: " + " → ".join(
        (C["b"] + e["stage"] + C["0"]) if e["stage"] == t["stage"] else e["stage"]
        for e in pipe))
    if t["brief"]:
        brief = t["brief"] if args.full else _clip(t["brief"], 4000, "`--full` for all of it")
        _p(f"\n{C['dim']}brief{C['0']}\n" + textwrap.indent(brief, "  "))
    if t["acceptance"]:
        _p(f"\n{C['dim']}acceptance{C['0']}")
        for a in t["acceptance"]:
            _p("  - " + a)
    bl = B.blockers(db, cfg, wfs, t)
    if bl:
        _p(f"\n{C['y']}blocked by{C['0']}")
        for b in bl:
            _p("  - " + b)
    if t["last_evidence"]:
        ev = str(t["last_evidence"])
        _p(f"\n{C['y']}last returned because{C['0']}\n" +
           textwrap.indent(ev if args.full else _clip(ev, 2000, "`--full` for all of it"), "  "))
    if t["status"] == B.RUNNING:
        _p(f"\n{C['dim']}an agent is working this card now. Edits you make land "
           f"on its next attempt or stage — the running agent already has its "
           f"prompt.{C['0']}")
    runs = db.q("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 5",
                (t["id"],))
    if runs:
        _p(f"\n{C['dim']}runs{C['0']}")
        for r in runs:
            _p(f"  {r['id']} {r['stage']}/{r['agent_type']} exit={r['exit_code']} "
               f"${(r['usd'] or 0):.3f} {r['log_dir']}")
    return 0


def cmd_edit(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    t = B.get(db, args.id)
    if not t:
        _p(f"{C['r']}no such card{C['0']}")
        return 1
    fields: dict[str, Any] = {}
    if args.title:
        fields["title"] = args.title
    if args.brief:
        fields["brief"] = args.brief
    if args.append_brief:
        fields["brief"] = t["brief"] + "\n\n" + args.append_brief
    if args.accept:
        fields["acceptance"] = (list(t["acceptance"]) + args.accept) if args.add \
            else list(args.accept)
    if args.tag:
        fields["tags"] = sorted(set(list(t["tags"]) + args.tag))
    if args.priority is not None:
        fields["priority"] = args.priority
    if args.max_attempts is not None:
        fields["max_attempts"] = args.max_attempts
    if args.type:
        fields["card_type"] = args.type
    if args.model:
        fields["model"] = None if args.model.lower() == "default" else args.model
    if args.scope:
        ws = dict(t["workspace"])
        ws["scope"] = list(args.scope)
        fields["workspace"] = ws
    if args.requeue:
        fields.update({"status": B.QUEUED, "attempts": 0, "defer_until": 0,
                       "defer_reason": None, "block_reason": None})
    if not fields:
        _p("nothing to change")
        return 1
    B.update(db, args.id, actor="human", **fields)
    _p(f"{C['g']}updated{C['0']} {args.id}: {', '.join(sorted(fields))}")
    return 0


def cmd_start(args) -> int:
    _root, db, _cfg, wfs = _ctx(args)
    for tid in args.ids:
        B.start_card(db, wfs, tid)
        _p(f"started {tid}")
    return 0


def cmd_link(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    try:
        B.link(db, args.src, args.dst, args.kind)
    except ValueError as e:
        _p(f"{C['r']}{e}{C['0']}")
        return 1
    _p(f"{args.src} --{args.kind}--> {args.dst}")
    return 0


def cmd_unlink(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    before = len(B.deps_of(db, args.dst, args.kind))
    B.unlink(db, args.src, args.dst, args.kind)
    after = len(B.deps_of(db, args.dst, args.kind))
    if before == after and args.kind == "finish_to_start":
        _p(f"{C['y']}no {args.kind} edge from {args.src} to {args.dst}{C['0']}")
        return 1
    _p(f"{C['g']}removed{C['0']} {args.src} --{args.kind}--> {args.dst}")
    return 0


def cmd_edges(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    rows = db.q("SELECT src, dst, kind FROM edges ORDER BY kind, src")
    if args.id:
        rows = [r for r in rows if args.id in (r["src"], r["dst"])]
    if not rows:
        _p(f"{C['dim']}no edges{C['0']}")
        return 0
    titles = {t["id"]: t["title"] for t in B.all_tasks(db)}
    for r in rows:
        _p(f"{r['src']} {C['dim']}--{r['kind']}-->{C['0']} {r['dst']}  "
           f"{C['dim']}{titles.get(r['dst'], '')[:44]}{C['0']}")
    return 0


def cmd_cancel(args) -> int:
    """Names the casualties before it makes them. Cancelling a parent used to
    take its children silently, which is an easy way to lose a running card."""
    _root, db, _cfg, _wfs = _ctx(args)
    rc = 0
    for tid in args.ids:
        t = B.get(db, tid)
        if not t:
            _p(f"{C['r']}no such card{C['0']} {tid}")
            rc = 1
            continue
        kids = [k for k in B.subtree_ids(db, tid) if k != tid]
        live = [k for k in kids
                if (B.get(db, k) or {}).get("status") not in B.TERMINAL]

        if live and not args.cascade and not args.only:
            _p(f"{C['y']}{tid} has {len(live)} unfinished card(s) beneath it{C['0']}")
            for k in live:
                kt = B.get(db, k) or {}
                mark = f"{C['g']}● {C['0']}" if kt.get("status") == B.RUNNING else "  "
                _p(f"  {mark}{k}  {kt.get('stage',''):<10} "
                   f"{kt.get('status',''):<11} {(kt.get('title') or '')[:48]}")
            _p(f"cancel them too with {C['b']}--cascade{C['0']}, or "
               f"{C['b']}--only{C['0']} to cancel just {tid}")
            rc = 1
            continue

        doomed = [tid] + (live if args.cascade else [])
        B.cancel(db, tid, cascade=bool(args.cascade),
                 reason=getattr(args, "reason", None))
        _p(f"{C['g']}cancelled{C['0']} {', '.join(doomed)}")
    return rc


def cmd_blocked(args) -> int:
    _root, db, cfg, wfs = _ctx(args)
    any_blocked = False
    for t in B.all_tasks(db, include_terminal=False):
        bl = B.blockers(db, cfg, wfs, t)
        if not bl:
            continue
        any_blocked = True
        _p(f"{C['b']}{t['id']}{C['0']} {t['title'][:60]} {C['dim']}({t['stage']}){C['0']}")
        for b in bl:
            _p(f"    {C['y']}—{C['0']} {b}")
    if not any_blocked:
        _p(f"{C['g']}nothing blocked{C['0']} — every unfinished card is running or ready")
    return 0


# ---------------------------------------------------------------------------
# checkpoints and proposals
# ---------------------------------------------------------------------------

def cmd_needs(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    rows = db.q("SELECT * FROM checkpoints WHERE status='open' ORDER BY created_at")
    if not rows:
        _p(f"{C['g']}nothing waiting on you{C['0']}")
        return 0
    for r in rows:
        b = json.loads(r["bundle"] or "{}")
        _p(f"{C['y']}{r['id']}{C['0']}  {r['question']}")
        _p(f"  {C['dim']}card {r['task_id']} · "
           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created_at']))}{C['0']}")
        if b.get("summary"):
            _p(textwrap.indent(str(b["summary"])[:700], "  "))
        if b.get("evidence"):
            _p(f"  {C['dim']}evidence{C['0']}")
            _p(textwrap.indent(str(b["evidence"])[:700], "    "))
        if b.get("changed_files"):
            _p(f"  {C['dim']}{len(b['changed_files'])} file(s): "
               f"{', '.join(b['changed_files'][:6])}{C['0']}")
        _p(f"  {C['dim']}dispatch respond {r['id']} approve|amend|reject "
           f"--note \"...\"{C['0']}\n")
    return 0


def cmd_respond(args) -> int:
    _root, db, cfg, wfs = _ctx(args)
    row = db.q1("SELECT audience, topic FROM checkpoints WHERE id=?", (args.id,))
    if row and args.actor == "session" and (row["audience"] or "any") == "human":
        _p(f"{C['r']}this one is not a session's to answer{C['0']} — "
           f"'{row['topic'] or 'it'}' is on session.human_only.")
        _p(f"{C['dim']}relay it to the operator and stop.{C['0']}")
        return 1
    if args.response == "reject" and not args.note:
        _p(f"{C['y']}a rejection with no reason gives the agent nothing to work "
           f"with — pass --note{C['0']}")
        return 1
    B.resolve_checkpoint(db, cfg, wfs, args.id, args.response, args.note or "",
                         actor=args.actor)
    _p(f"{C['g']}recorded{C['0']} {args.response}")
    return 0


def cmd_propose(args) -> int:
    """Called *by agents*, from inside their worktree."""
    _root, db, _cfg, _wfs = _ctx(args)
    payload: dict[str, Any] = {}
    if args.title:
        payload["title"] = args.title
    if args.accept:
        payload["acceptance"] = args.accept
    if args.scope:
        payload["scope"] = args.scope
    if args.kind in ("add_task", "split") and not args.accept:
        _p(f"{C['r']}--accept is required for {args.kind}{C['0']} — a card with "
           f"nothing to check is refused before it runs, so proposing one just "
           f"spends a human's attention.")
        _p(f"{C['dim']}give at least one, ideally a runnable command:"
           f"\n  --accept \"pytest tests/test_x.py passes\"{C['0']}")
        return 1
    if args.brief:
        payload["brief"] = args.brief
    if args.reason:
        payload["reason"] = args.reason
    if args.append:
        payload["append"] = args.append
    if args.task:
        payload["task_id"] = args.task
    if args.src and args.dst:
        payload.update({"src": args.src, "dst": args.dst})
    if args.gate:
        payload["gate"] = args.gate
    if args.json:
        payload.update(json.loads(args.json))
    src = args.from_task or os.environ.get("DISPATCH_TASK_ID")
    try:
        pid = P.submit(db, from_task=src, kind=args.kind, payload=payload,
                       rationale=args.rationale or "", confidence=args.confidence,
                       urgency=args.urgency)
    except ValueError as e:
        _p(f"{C['r']}{e}{C['0']}")
        return 1
    _p(f"{C['g']}proposed{C['0']} {pid} ({args.kind}) — the adjudicator decides; "
       f"you are not writing to the board directly")
    return 0


def cmd_proposals(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    rows = db.q("SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?",
                (args.limit,))
    for r in rows:
        col = {"accepted": C["g"], "rejected": C["dim"], "escalated": C["y"]}.get(
            r["status"], "")
        _p(f"{col}{r['id']}{C['0']} {r['kind']:<14} {r['status']:<10} "
           f"{C['dim']}{(r['tier'] or '—'):<8}{C['0']} {(r['rationale'] or '')[:60]}")
        if r["decision"]:
            _p(f"    {C['dim']}{r['decision'][:110]}{C['0']}")
    return 0


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------

def cmd_workflows(args) -> int:
    root, db, cfg, wfs = _ctx(args)
    if args.action == "export":
        p = W.export_file(root, db)
        _p(f"wrote {p}")
    elif args.action == "import":
        wfs = W.import_file(root, db, args.file)
        problems = W.validate(wfs, cfg, load_agents(root))
        _p(f"imported {len(wfs)} card type(s)")
        for pr in problems:
            _p(f"  {C['y']}—{C['0']} {pr}")
    else:
        for ct, wf in wfs.items():
            _p(f"{C['b']}{ct}{C['0']} {C['dim']}({wf.get('label')}){C['0']}")
            for i, e in enumerate(wf.get("stages", []), 1):
                gates = ", ".join(str(g) for g in e.get("gates", [])) or "—"
                lock = f" lock={e['lock']}" if e.get("lock") else ""
                agent = str(e.get("agent"))
                _p(f"  {i}. {e['stage']:<11} {C['c']}{agent:<12}{C['0']}"
                   f" gates: {gates}{lock}")
        problems = W.validate(wfs, cfg, load_agents(root))
        for pr in problems:
            _p(f"{C['y']}—{C['0']} {pr}")
    return 0


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def cmd_up(args) -> int:
    from dispatch.scheduler import Scheduler, clear_pid, read_pid, write_pid
    from dispatch.server import serve

    root = find_root(getattr(args, "root", None))
    db = open_db(root)
    cfg = load_config(root)
    p = paths(root)

    existing = read_pid(root)
    if existing:
        _p(f"{C['y']}scheduler already running{C['0']} (pid {existing})")
        return 1

    if args.detach:
        cmd = [sys.executable, "-m", "dispatch", "up"]
        if args.no_web:
            cmd.append("--no-web")
        if args.port:
            cmd += ["--port", str(args.port)]
        if args.host:
            cmd += ["--host", args.host]
        with open(p["log"], "a") as logf:
            proc = subprocess.Popen(cmd, cwd=root, stdout=logf, stderr=logf,
                                    start_new_session=True)
        _p(f"{C['g']}scheduler detached{C['0']} pid {proc.pid} · log {p['log']}")
        if not args.no_web:
            from dispatch import net as N
            host, _ = N.resolve_host(cfg, args.host)
            port = N.resolve_port(cfg, root, args.port)
            for url in N.display_urls(host, port):
                _p(f"board: {url}")
        return 0

    write_pid(root)
    # deliberately not a context manager: it lives as long as the daemon
    logf = open(p["log"], "a", buffering=1)  # noqa: SIM115

    echo = sys.stdout.isatty()   # detached mode already redirects stdout here

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        if echo:
            print(line, flush=True)
        logf.write(line + "\n")

    web: dict[str, Any] = {"httpd": None}
    if not args.no_web:
        from dispatch import net as N
        bind, warnings = N.resolve_host(cfg, args.host)
        first = N.resolve_port(cfg, root, args.port)
        for w in warnings:
            log(w)

        def bring_up_board() -> None:
            for port in range(first, first + 12):
                try:
                    web["httpd"] = serve(root, db, bind, port, block=False)
                except OSError:
                    continue
                urls = N.display_urls(bind, port)
                db.set_meta("board_url", urls[0])
                db.set_meta("board_bind", f"{bind}:{port}")
                if port != first:
                    log(f"port {first} was taken — board at {urls[0]}")
                else:
                    log(f"board at {urls[0]}")
                for extra in urls[1:]:
                    log(f"        also {extra}")
                return
            log(f"web board unavailable: ports {first}-{first + 11} all in use")

        # In its own thread: whatever the board does, the loop starts now.
        threading.Thread(target=bring_up_board, daemon=True).start()

    sched = Scheduler(root, db, log=log)
    try:
        sched.run_forever()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        sched.stop_flag.set()
        if web["httpd"]:
            web["httpd"].shutdown()
        clear_pid(root)
        logf.close()
    return 0


def cmd_down(args) -> int:
    import signal

    from dispatch.scheduler import clear_pid, read_pid
    root = find_root(getattr(args, "root", None))
    pid = read_pid(root)
    if not pid:
        _p("scheduler is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    clear_pid(root)
    _p(f"{C['g']}stopped{C['0']} pid {pid}")
    return 0


def cmd_serve(args) -> int:
    from dispatch import net as N
    from dispatch.server import serve
    root = find_root(getattr(args, "root", None))
    db = open_db(root)
    cfg = load_config(root)
    bind, warnings = N.resolve_host(cfg, args.host)
    first = N.resolve_port(cfg, root, args.port)
    for w in warnings:
        _p(f"{C['y']}{w}{C['0']}")
    for port in range(first, first + 12):
        if not N.port_is_free(bind, port):
            continue
        urls = N.display_urls(bind, port)
        db.set_meta("board_url", urls[0])
        db.set_meta("board_bind", f"{bind}:{port}")
        for url in urls:
            _p(f"board at {C['c']}{url}{C['0']}")
        _p(f"{C['dim']}ctrl-c to stop{C['0']}")
        try:
            serve(root, db, bind, port, block=True)
        except KeyboardInterrupt:
            pass
        return 0
    _p(f"{C['r']}ports {first}-{first + 11} are all in use{C['0']}")
    return 1


def cmd_tick(args) -> int:
    """One tick, in the foreground. The way to see what the scheduler is doing."""
    from dispatch.scheduler import Scheduler
    root = find_root(getattr(args, "root", None))
    db = open_db(root)
    sched = Scheduler(root, db, log=lambda m: _p(f"{C['dim']}{m}{C['0']}"))
    n = args.count
    for i in range(n):
        sched.tick()
        if args.wait and i < n - 1:
            time.sleep(sched.cfg["scheduler"].get("tick_seconds", 5))
    if args.wait:
        deadline = time.time() + args.wait
        while time.time() < deadline and any(t.is_alive() for t in sched._threads.values()):
            time.sleep(2)
            sched.tick()
    _p(f"{C['dim']}{n} tick(s) done{C['0']}")
    return 0


def cmd_status(args) -> int:
    from dispatch.scheduler import read_pid
    root, db, cfg, _wfs = _ctx(args)
    pid = read_pid(root)
    leased = db.q("SELECT * FROM leases")
    counts = {r["status"]: r["c"] for r in
              db.q("SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
    spend = B.spend(db)
    ratio, created, done = P.expansion_ratio(db, cfg)
    open_cp = db.q1("SELECT COUNT(*) c FROM checkpoints WHERE status='open'")["c"]

    from dispatch import net as N
    from dispatch import sandbox as SB

    paused = bool(cfg["scheduler"].get("paused"))
    why = cfg["scheduler"].get("paused_reason")
    dot = (f"{C['y']}◐{C['0']}" if paused else f"{C['g']}●{C['0']}") if pid \
        else f"{C['dim']}○{C['0']}"
    state = ("paused" if paused else "running") if pid else "down"
    _p(f"{dot} scheduler {state}" + (f" (pid {pid})" if pid else ""))
    if paused:
        # a deliberate safety pause and a crash used to look identical here
        _p(f"  {C['y']}paused{C['0']}    {why or 'paused by hand'}")
        _p(f"  {C['dim']}          answer it in `dispatch needs`, or "
           f"`dispatch resume`{C['0']}")
    _p(f"  root      {root}")
    from dispatch.config import auth_note
    note = auth_note(cfg)
    colour = C["y"] if ("will be removed" in note or "not set" in note) else C["dim"]
    _p(f"  billing   {colour}{note}{C['0']}")
    url = db.get_meta("board_url")
    if url and pid:
        _p(f"  board     {C['c']}{url}{C['0']}")
    elif not pid:
        host, _ = N.resolve_host(cfg)
        _p(f"  board     {C['dim']}{N.display_urls(host, N.resolve_port(cfg, root))[0]}"
           f" (when up){C['0']}")
    ok, problems = SB.preflight(cfg, root)
    marks = [f for f in os.listdir(paths(root)["root"])
             if f.startswith("channel-watermark-")] \
        if os.path.isdir(paths(root)["root"]) else []
    live = []
    for m in marks:
        pid_s = m.rsplit("-", 1)[-1]
        if pid_s.isdigit():
            try:
                os.kill(int(pid_s), 0)
                live.append(pid_s)
            except OSError:
                pass
    if live:
        _p(f"  channels  {len(live)} session(s) attached "
           f"{C['dim']}(pid {', '.join(live)}){C['0']}")

    if SB.enabled(cfg) and ok:
        _p(f"  sandbox   {C['g']}on{C['0']} — {SB.describe(cfg)}")
    elif not SB.enabled(cfg):
        _p(f"  sandbox   {C['y']}off{C['0']} — agents are not confined")
    for pr in problems:
        _p(f"            {C['y']}{pr}{C['0']}")
    _p(f"  in flight {len(leased)}/{cfg['scheduler']['max_concurrent']}")
    for r in leased:
        t = B.get(db, r["task_id"])
        _p(f"    {C['g']}●{C['0']} {r['task_id']} {r['stage']:<10} "
           f"{(t['title'][:44] if t else '')}")
    _p("  cards     " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for t in db.q("SELECT id,defer_reason FROM tasks WHERE status='merging' "
                  "AND defer_reason IS NOT NULL AND defer_reason != ''"):
        why = t["defer_reason"]
        why = why[len("merge: "):] if why.startswith("merge: ") else why
        _p(f"  {C['y']}stuck{C['0']}     {t['id']} cannot land — "
           f"{why.splitlines()[0][:80]}")
    total_cap = cfg.get("containment", {}).get("total_budget_usd")
    total = float(spend["usd"] or 0)
    cap_note = ""
    if total_cap is not None:
        over = total >= float(total_cap)
        cap_note = (f"  {C['r'] if over else C['dim']}of ${float(total_cap):.2f}"
                    f"{' — CEILING REACHED' if over else ''}{C['0']}")
    judged = (f", {spend['arbiter_calls']} arbiter call(s) "
              f"${spend['arbiter_usd']:.2f}") if spend["arbiter_calls"] else ""
    _p(f"  spend     ${total:.2f} over {spend['runs']} run(s)"
       f"{judged}{cap_note}")
    if total_cap is None and total > 0:
        _p(f"  {C['dim']}          per-subtree ceilings only; set "
           f"containment.total_budget_usd for a board-wide one{C['0']}")
    if ratio:
        warn = C["r"] if ratio > cfg["containment"]["expansion_ratio_limit"] else ""
        _p(f"  expansion {warn}{ratio:.2f}×{C['0']} "  # noqa: RUF001
           f"({created} created / {done} completed, recent)")
    if open_cp:
        _p(f"  {C['y']}needs you {open_cp} checkpoint(s){C['0']} — `dispatch needs`")
    return 0


def cmd_log(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    rows = db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,))
    for r in reversed(rows):
        data = json.loads(r["data"] or "{}")
        bits = " ".join(f"{k}={str(v)[:60]}" for k, v in data.items() if v not in (None, ""))
        _p(f"{C['dim']}{time.strftime('%H:%M:%S', time.localtime(r['ts']))}{C['0']} "
           f"{C['c']}{r['kind']:<24}{C['0']} {(r['task_id'] or ''):<10} "
           f"{C['dim']}{bits[:110]}{C['0']}")
    return 0


def cmd_docs(args) -> int:
    from dispatch import help as H

    if args.search:
        hits = H.search(args.search)
        if not hits:
            _p(f"{C['dim']}nothing in the manual mentions "
               f"'{args.search}'{C['0']}")
            return 1
        seen = set()
        for topic, line_no, line in hits[:60]:
            if topic not in seen:
                seen.add(topic)
                title, _ = H.summary(topic)
                _p(f"\n{C['b']}{topic}{C['0']} {C['dim']}— {title}{C['0']}")
            _p(f"  {C['dim']}{line_no:>4}{C['0']}  {line[:96]}")
        _p(f"\n{C['dim']}read one: dispatch docs {sorted(seen)[0]}{C['0']}")
        return 0

    if args.export:
        return _export_docs(args.export)

    if args.all:
        _p(H.whole_manual())
        return 0

    if not args.topic:
        _p(H.index())
        return 0

    topic = H.resolve(args.topic)
    if not topic:
        _p(f"{C['r']}no such topic '{args.topic}'{C['0']}")
        _p(f"{C['dim']}have: {', '.join(H.available())}{C['0']}")
        return 1

    body = H.read(topic) or ""
    if args.page:
        chunk, page, total = H.paginate(body, args.page, args.lines)
        _p(H.render(chunk))
        if total > 1:
            nxt = (f"dispatch docs {topic} --page {page + 1}"
                   if page < total else "(end)")
            _p(f"\n{C['dim']}— page {page}/{total} — {nxt}{C['0']}")
        return 0

    _p(H.render(body))
    return 0


def cmd_wait(args) -> int:
    """Block until cards land. One tool call for a session, instead of a poll
    loop that spends a turn every few minutes to learn nothing."""
    from dispatch import watch as W
    _root, db, _cfg, _wfs = _ctx(args)

    ids = W.targets(db, args.ids or None, tag=args.tag, card_type=args.card_type)
    if not ids:
        _p(f"{C['dim']}nothing to wait for{C['0']}")
        return W.OK

    if not args.quiet:
        _p(f"{C['dim']}waiting on {len(ids)} card(s)"
           f"{f', up to {int(args.timeout)}s' if args.timeout else ''}{C['0']}")

    def report(tid, was, now):
        if args.quiet or args.json:
            return
        mark = {"done": C["g"] + "✓" + C["0"],
                "deadletter": C["r"] + "✗" + C["0"],
                "failed": C["r"] + "✗" + C["0"],
                "checkpoint": C["y"] + "?" + C["0"]}.get(now["status"],
                                                         C["dim"] + "·" + C["0"])
        when = time.strftime("%H:%M:%S")
        _p(f"  {C['dim']}{when}{C['0']} {mark} {tid} "
           f"{now['stage']}/{now['status']}  {now['title'][:48]}")

    code, reason = W.wait(db, ids, timeout=args.timeout, interval=args.interval,
                          stop_on_checkpoint=not args.through_checkpoints,
                          on_change=report)

    if args.json:
        _p(json.dumps({"exit": code, "reason": reason,
                       "cards": W.snapshot(db, ids),
                       "checkpoints": W.open_checkpoints(db, ids)}, indent=2))
        return code

    colour = {W.OK: C["g"], W.FAILED: C["r"], W.TIMEOUT: C["y"],
              W.NEEDS_HUMAN: C["y"]}.get(code, "")
    _p(f"{colour}{reason}{C['0']}")
    if code == W.NEEDS_HUMAN:
        for cp in W.open_checkpoints(db, ids):
            _p(f"  {C['y']}{cp['id']}{C['0']} {cp['question'][:90]}")
        _p(f"{C['dim']}dispatch needs   # the full context{C['0']}")
    return code


def cmd_channel(args) -> int:
    """Run as a Claude Code channel, or wire one up.

    The only way to reach into a session that is already running. What crosses
    is a pointer — "card X needs a response" — never the agent-authored content
    behind it.
    """
    from dispatch.channel import Channel
    root = find_root(getattr(args, "root", None))

    if args.install:
        return _install_channel(root)

    if not os.path.isdir(paths(root)["root"]):
        print("dispatch channel: no board here", file=sys.stderr)
        return 1
    return Channel(root, poll=args.poll).serve()


def _install_channel(root: str) -> int:
    """Add the server to .mcp.json and print how to launch a session with it."""
    p = os.path.join(root, ".mcp.json")
    try:
        with open(p) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = {}
    servers = cfg.setdefault("mcpServers", {})
    already = servers.get("dispatch")
    servers["dispatch"] = {"command": "dispatch",
                           "args": ["--root", root, "channel"]}
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    _p(f"{C['g']}{'updated' if already else 'added'}{C['0']} dispatch in {p}")
    _p()
    _p("Start a session with the channel registered:")
    _p(f"  {C['b']}claude --dangerously-load-development-channels "
       f"server:dispatch{C['0']}")
    _p()
    _p(f"{C['dim']}The development flag is needed because custom channels are "
       f"not on\nAnthropic's allowlist during the research preview. It skips "
       f"that list only.\nChannels also need claude.ai or Console auth — not "
       f"Bedrock or Vertex.{C['0']}")
    _p()
    _p(f"{C['dim']}Events are pointers. The session still reads the decision "
       f"with\n`dispatch attend`, so agent-written text never arrives as an "
       f"event.{C['0']}")
    return 0


def cmd_attend(args) -> int:
    """Sit in the loop so a person does not have to.

    A session that seeded the cards holds the context of the larger task, which
    makes it better placed than the operator to answer "does this satisfy what
    we were doing" — and faster by minutes. Money, secrets and runaway
    detection stay a person's call.
    """
    from dispatch import watch as W
    _root, db, _cfg, _wfs = _ctx(args)
    code, packet = W.attend(db, timeout=args.timeout, interval=args.interval,
                            audience=args.audience)

    if args.json:
        _p(json.dumps({"exit": code, **(packet or {})}, indent=2, default=str))
        return code

    if code == W.TIMEOUT:
        rows = (packet or {}).get("working") or []
        _p(f"{C['dim']}still working — {len(rows)} card(s) in flight. "
           f"Call `dispatch attend` again.{C['0']}")
        for r in rows[:6]:
            _p(f"  {r['id']}  {r['stage']}/{r['status']}  {r['title'][:56]}")
        return code

    if code == W.OK:
        _p(f"{C['g']}the board is idle and nothing is waiting{C['0']}")
        _p(f"{C['dim']}Check the result against what you were asked for. If it "
           f"is short, add cards; if it is done, say so and stop.{C['0']}")
        return code

    if code == W.FAILED:
        _p(f"{C['r']}cards ended badly and nothing is open about them{C['0']}")
        for f in (packet or {}).get("failed", []):
            _p(f"  {f['id']}  {f['title'][:60]}")
            if f.get("reason"):
                _p(f"      {C['dim']}{f['reason']}{C['0']}")
        return code

    _p(_render_packet(packet, relay=(code == W.RELAY), full=args.full))
    return code


def _render_packet(p: dict[str, Any], relay: bool = False,
                   full: bool = False) -> str:
    """Written to be read by a model: everything needed, nothing to go find."""
    cp, card, w = p["checkpoint"], p["card"], p["what_happened"]
    out = []
    if relay:
        out.append(f"{C['y']}This one is not yours to decide — only a person "
                   f"may answer it. Relay it and stop.{C['0']}\n")
    out.append(f"{C['b']}{cp['question']}{C['0']}")
    topic = cp.get("topic")
    label = cp["kind"] + (f" · {topic}" if topic and topic != cp["kind"] else "")
    out.append(f"{C['dim']}{cp['id']} · {label} · "
               f"waiting {int((time.time() - cp['waiting_since']) / 60)}m{C['0']}")

    out.append(f"\n{C['dim']}── the card ──{C['0']}")
    out.append(f"{card['id']}  {card['title']}")
    out.append(f"{C['dim']}{card['card_type']} · {card['stage']} · "
               f"attempt {card['attempts']}"
               f"{' · ' + card['branch'] if card.get('branch') else ''}{C['0']}")
    if card.get("brief"):
        out.append("\n" + textwrap.indent(card["brief"], "  "))
    if card.get("acceptance"):
        out.append(f"\n{C['dim']}it is judged against exactly these:{C['0']}")
        for a in card["acceptance"]:
            out.append(f"  - {a}")

    out.append(f"\n{C['dim']}── what happened ──{C['0']}")
    # Never clipped: the verdict survives a clip and the caveat does not, and a
    # reviewer's "one significant concern" lives at the end of the account.
    for key, label in (("summary", "the agent's account"),
                       ("evidence", "evidence"), ("reason", "reason"),
                       ("note", "note")):
        if w.get(key):
            out.append(f"\n{C['dim']}{label}{C['0']}")
            out.append(textwrap.indent(str(w[key]), "  "))
    if w.get("gates"):
        out.append(f"\n{C['dim']}gates{C['0']}")
        for g in w["gates"][:8]:
            mark = C["g"] if g["verdict"] == "pass" else C["r"]
            out.append(f"  {mark}{g['verdict']:<8}{C['0']} {g['gate']}  "
                       f"{C['dim']}{(g.get('reason') or '')[:60]}{C['0']}")
    if w.get("changed_files"):
        out.append(f"\n{C['dim']}{len(w['changed_files'])} changed file(s){C['0']}")
        for f in w["changed_files"][:20]:
            out.append(f"  {f}")
    if w.get("diff"):
        diff = str(w["diff"])
        out.append(f"\n{C['dim']}diff{C['0']}")
        if full or len(diff) <= 8000:
            out.append(textwrap.indent(diff, "  "))
        else:
            where = w.get("diff_file")
            remedy = "`dispatch attend --full`" + (f", or read {where}" if where else "")
            out.append(textwrap.indent(_clip(diff, 8000, remedy), "  "))
    if w.get("plan"):
        out.append(f"\n{C['dim']}the proposed plan is on the card — "
                   f"`dispatch plan {card['id']}`{C['0']}")

    if not relay:
        out.append(f"\n{C['dim']}── decide ──{C['0']}")
        for opt, gloss in (("approve", "the work stands"),
                           ("amend", "your note becomes the next attempt's brief"),
                           ("reject", "send it back a stage with your reason")):
            if opt in p.get("options", []):
                out.append(f"  dispatch respond {cp['id']} {opt}"
                           + (' --note "..."' if opt != "approve" else "")
                           + f"  {C['dim']}# {gloss}{C['0']}")
        out.append(f"  {C['dim']}--as session records that you decided it, "
                   f"not a person{C['0']}")
    return "\n".join(out)


def cmd_hook(args) -> int:
    """Wired as a Claude Code Stop hook, this is how the board reaches back
    into a session — MCP cannot, since it only answers questions it is asked."""
    from dispatch import watch as W
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    session = str(payload.get("session_id") or "anonymous")

    root = find_root(getattr(args, "root", None))
    try:
        db = open_db(root)
    except SystemExit:
        return 0                      # no board here: never obstruct a session

    summary = W.board_summary(db)
    text = W.summary_text(summary)

    if not args.block_while_busy or summary["idle"]:
        _reset_blocks(root, session)
        print(json.dumps({"hookSpecificOutput": {"additionalContext": text}}))
        return 0

    # Blocking mode. Claude Code passes no loop guard for Stop hooks, so keep
    # our own count and let go before a session can spin forever.
    n = _bump_blocks(root, session)
    if n > args.max_blocks:
        _reset_blocks(root, session)
        print(json.dumps({"hookSpecificOutput": {"additionalContext":
              text + f"\n\n(dispatch stopped holding this session open after "
                     f"{args.max_blocks} turns.)"}}))
        return 0
    print(text + "\n\nThe dispatch board is still working. Run "
                 "`dispatch wait --all --timeout 600` to block until it "
                 "settles, or answer any checkpoints above.", file=sys.stderr)
    return 2                          # exit 2: do not stop, keep going


def _blocks_path(root: str) -> str:
    return os.path.join(paths(root)["root"], "stop-hook-blocks.json")


def _read_blocks(root: str) -> dict[str, int]:
    try:
        with open(_blocks_path(root)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _bump_blocks(root: str, session: str) -> int:
    data = _read_blocks(root)
    data[session] = int(data.get(session, 0)) + 1
    try:
        with open(_blocks_path(root), "w") as f:
            json.dump(data, f)
    except OSError:
        pass
    return data[session]


def _reset_blocks(root: str, session: str) -> None:
    data = _read_blocks(root)
    if data.pop(session, None) is not None:
        try:
            with open(_blocks_path(root), "w") as f:
                json.dump(data, f)
        except OSError:
            pass


def cmd_intent(args) -> int:
    """Describe what you want. An agent reads the repo and proposes a plan;
    you approve it; the cards come from that."""
    _root, db, cfg, wfs = _ctx(args)
    if "intent" not in wfs:
        _p(f"{C['r']}this board has no 'intent' card type{C['0']} — "
           f"run `dispatch upgrade --apply`, or add one in the Workflows tab")
        return 1
    text = args.text
    if not text or text == "-":
        _p(f"{C['dim']}describe what you want, then ctrl-d{C['0']}")
        text = sys.stdin.read().strip()
    if not text:
        _p(f"{C['r']}nothing to plan{C['0']}")
        return 1
    title = args.title or (text.strip().splitlines()[0][:72])
    tid = B.create(db, cfg, wfs, title=title, brief=text, card_type="intent",
                   acceptance=["a plan you would approve"],
                   priority=args.priority, provenance="human")
    B.start_card(db, wfs, tid)
    _p(f"{C['g']}{tid}{C['0']}  {title}")
    _p(f"{C['dim']}a planner will read the repo and propose cards. "
       f"Watch with `dispatch wait {tid}` — it returns when the plan needs "
       f"you.{C['0']}")
    return 0


def cmd_plan(args) -> int:
    _root, db, _cfg, _wfs = _ctx(args)
    t = B.get(db, args.id)
    if not t:
        _p(f"{C['r']}no such card{C['0']}")
        return 1
    plan = t.get("plan")
    if not plan:
        _p(f"{C['dim']}no plan on this card yet{C['0']}")
        return 1
    if isinstance(plan, str):
        plan = json.loads(plan)
    if args.json:
        _p(json.dumps(plan, indent=2))
        return 0
    if plan.get("summary"):
        _p(textwrap.fill(plan["summary"], 88))
    cards = plan.get("cards") or []
    _p(f"\n{C['b']}{len(cards)} card(s){C['0']}")
    for c in cards:
        dep = c.get("depends_on") or []
        _p(f"\n  {C['c']}{c.get('ref','?')}{C['0']} {C['b']}{c.get('title')}{C['0']}"
           + (f"  {C['dim']}after {', '.join(dep)}{C['0']}" if dep else ""))
        if c.get("brief"):
            _p(textwrap.indent(
                c["brief"] if args.full else _clip(
                    c["brief"], 400, "`--full` for all of it"), "      "))
        for a in c.get("acceptance") or []:
            _p(f"      {C['g']}✓{C['0']} {a}")
        if c.get("scope"):
            _p(f"      {C['dim']}scope: {', '.join(c['scope'])}{C['0']}")
    for key, colour in (("risks", C["y"]), ("out_of_scope", C["dim"])):
        if plan.get(key):
            _p(f"\n{colour}{key.replace('_', ' ')}{C['0']}")
            for line in plan[key]:
                _p(f"  - {line}")
    cp = db.q1("SELECT id FROM checkpoints WHERE task_id=? AND status='open'",
               (args.id,))
    if cp:
        _p(f"\n{C['dim']}dispatch respond {cp['id']} approve   # create these cards"
           f"\ndispatch respond {cp['id']} amend --note \"...\"   # re-plan{C['0']}")
    return 0


def cmd_memory(args) -> int:
    from dispatch import memory as MEM
    _root, db, _cfg, _wfs = _ctx(args)
    action = args.action

    if action == "add":
        mid = MEM.add(db, title=args.title, body=args.body or "",
                      tags=args.tags.split(",") if args.tags else [],
                      kind=args.kind,
                      source_task=args.source or os.environ.get("DISPATCH_TASK_ID"),
                      actor=os.environ.get("DISPATCH_TASK_ID") or "human")
        _p(f"{C['g']}{mid}{C['0']}  {args.title}")
        return 0

    if action == "rm":
        ok = MEM.delete(db, args.title)
        _p(f"{C['g']}removed{C['0']}" if ok else f"{C['r']}no such memory{C['0']}")
        return 0 if ok else 1

    if action == "show":
        m = MEM.get(db, args.title)
        if not m:
            _p(f"{C['r']}no such memory{C['0']}")
            return 1
        _p(f"{C['b']}{m['title']}{C['0']}  {C['dim']}{m['id']} · {m['kind']}"
           f"{(' · ' + ', '.join(m['tags'])) if m['tags'] else ''}{C['0']}")
        _p(textwrap.indent(m["body"], "  "))
        return 0

    found = (MEM.search(db, args.title or "", limit=args.limit,
                        tags=args.tags.split(",") if args.tags else None)
             if action == "search" and args.title
             else MEM.all_memories(db, limit=args.limit))
    if not found:
        _p(f"{C['dim']}nothing remembered yet{C['0']}")
        return 0
    for m in found:
        tags = (" " + " ".join("#" + t for t in m["tags"])) if m["tags"] else ""
        _p(f"{C['c']}{m['id']}{C['0']}  {C['b']}{m['title']}{C['0']} "
           f"{C['dim']}({m['kind']}){tags}{C['0']}")
        _p(textwrap.indent(
            _clip(m["body"], 300,
                  f"`dispatch memory show {m['id']}` for all of it"), "    "))
    return 0


def cmd_resume(args) -> int:
    """Clear a pause, including the expansion alarm's."""
    from dispatch import proposals as P
    root, db, cfg, _wfs = _ctx(args)
    was = cfg["scheduler"].get("paused")
    cfg["scheduler"]["paused"] = False
    cfg["scheduler"]["paused_reason"] = None
    save_config(root, cfg)
    if args.reset_expansion or "expansion" in str(cfg["scheduler"].get("paused_reason", "")):
        P.acknowledge_expansion(db, actor="human")
    P.acknowledge_expansion(db, actor="human")
    _p(f"{C['g']}resumed{C['0']}" if was else "was not paused")
    _p(f"{C['dim']}the expansion ratio is now measured from here{C['0']}")
    return 0


def cmd_upgrade(args) -> int:
    """Reconcile a board created by an older version with this one.

    Settings only — the database migrates itself, and nothing here touches a
    running scheduler: it re-reads config.json every tick, but it is still
    executing the code it was started with.
    """
    from dispatch.config import missing_settings, put, stale_settings
    from dispatch.scheduler import read_pid
    root = find_root(getattr(args, "root", None))
    cfg = load_config(root)

    missing = missing_settings(cfg)
    stale = stale_settings(cfg)
    if not missing and not stale:
        _p(f"{C['g']}already current{C['0']} — nothing to change")
        return 0

    if stale:
        _p(f"{C['b']}worth changing{C['0']}")
        for rec in stale:
            _p(f"  {C['c']}{rec['path']}{C['0']}: "
               f"{json.dumps(rec['current'])} → {json.dumps(rec['new'])}")
            _p(f"      {C['dim']}{rec['why']}{C['0']}")
    if missing:
        _p(f"\n{C['b']}new since this board was created{C['0']}")
        for path in missing[:24]:
            _p(f"  {C['dim']}{path}{C['0']}")
        if len(missing) > 24:
            _p(f"  {C['dim']}… and {len(missing) - 24} more{C['0']}")

    if not args.apply:
        _p(f"\n{C['dim']}nothing written — re-run with "
           f"{C['b']}--apply{C['0']}{C['dim']} to make these changes{C['0']}")
        return 0

    for rec in stale:
        put(cfg, rec["path"], rec["new"])
    save_config(root, cfg)          # writing merges in everything missing
    _write_agent_settings(root, cfg)
    _p(f"\n{C['g']}updated{C['0']} {paths(root)['config']}")

    pid = read_pid(root)
    if pid:
        _p(f"\n{C['y']}a scheduler is running (pid {pid}){C['0']}")
        _p("  Settings are re-read every tick, but the running process is still")
        _p("  executing the code it started with. Restart it when your current")
        _p(f"  cards are done: {C['b']}dispatch down && dispatch up -d{C['0']}")
        _p(f"  {C['dim']}Until then, confinement and the new gates are not in "
           f"effect for cards it dispatches.{C['0']}")
    return 0


def _export_docs(dest: str) -> int:
    """Write the manual out as browsable files.

    Generated, never hand-edited: `dispatch/docs/` is the single source, so the
    copy someone reads on the web cannot drift from the one an agent reads in
    the terminal.
    """
    from dispatch import help as H
    os.makedirs(dest, exist_ok=True)
    written = []
    for topic in H.available():
        body = H.read(topic) or ""
        with open(os.path.join(dest, topic + ".md"), "w") as f:
            f.write(body if body.endswith("\n") else body + "\n")
        written.append(topic)

    max(len(t) for t in written)
    lines = [
        "# dispatch — documentation",
        "",
        "Generated from the manual that ships inside the tool. Do not edit by",
        "hand: change `dispatch/docs/` and run `dispatch docs --export docs/`.",
        "",
        "The same pages are available in the terminal with `dispatch docs",
        "<topic>`, which is what agents read.",
        "",
    ]
    for topic in written:
        title, note = H.summary(topic)
        lines.append(f"- [{title}]({topic}.md) — {note}")
    notes_dir = os.path.join(dest, "notes")
    notes = sorted(f for f in os.listdir(notes_dir)
                   if f.endswith(".md")) if os.path.isdir(notes_dir) else []
    if notes:
        lines += ["", "## Notes", "",
                  "Point-in-time documents, not part of the manual and not kept",
                  "current.", ""]
        lines += [f"- [{n[:-3]}](notes/{n})" for n in notes]
    lines.append("")
    with open(os.path.join(dest, "README.md"), "w") as f:
        f.write("\n".join(lines))

    _p(f"{C['g']}wrote {len(written)} topic(s){C['0']} to {dest}/")
    return 0


def cmd_gc(args) -> int:
    """Remove worktrees for cards that are finished."""
    from dispatch.runner import remove_worktree
    root, db, _cfg, _wfs = _ctx(args)
    n = 0
    for t in B.all_tasks(db):
        if t["status"] in (B.DONE, B.CANCELLED):
            wt = (t.get("workspace") or {}).get("worktree")
            if wt and os.path.isdir(wt):
                remove_worktree(root, t["id"])
                n += 1
    _p(f"removed {n} worktree(s)")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dispatch",
        description="A kanban board that drives a fleet of coding agents. "
                    "The loop is code; the model is a subroutine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            typical session:
              dispatch docs                       the built-in manual
              dispatch init                       scaffold .dispatch/ in this repo
              dispatch add "..." --start          put a card on the board
              dispatch up                         run the scheduler + web board
              dispatch needs                      what is waiting on you
              dispatch blocked                    why nothing is running
        """))
    ap.add_argument("--version", action="version", version=f"dispatch {__version__}")
    ap.add_argument("--root", help="repo root (default: walk up from cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scaffold .dispatch/ in a repo")
    s.add_argument("path", nargs="?", help="repo path (default: cwd)")
    s.add_argument("--force", action="store_true",
                   help="overwrite config, workflows and agent prompts")
    s.add_argument("--git-init", action="store_true", help="git init if not a repo")
    s.add_argument("--test-cmd", help="command the tests_pass gate runs")
    s.add_argument("--lint-cmd", help="command the lint_clean gate runs")
    s.add_argument("--build-cmd", help="command the build_ok gate runs")
    s.add_argument("--no-verify", action="store_true",
                   help="store the test command without running it first")
    s.add_argument("--sandbox", action="store_true",
                   help="require confinement: refuse to run without it")
    s.add_argument("--no-sandbox", action="store_true",
                   help="run agents unconfined (not recommended)")
    s.add_argument("--auth", choices=["subscription", "api_key", "inherit"],
                   help="which credentials agents use (default: subscription)")
    s.add_argument("--sandbox-backend", choices=["auto", "seatbelt", "bwrap", "srt"],
                   help="auto (default) leaves the internet open; srt also "
                        "restricts egress to an allowlist")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add", help="create a card")
    s.add_argument("title")
    s.add_argument("--brief", default="", help="the literal prompt the agent receives")
    s.add_argument("--type", dest="card_type", default="development")
    s.add_argument("--accept", action="append", help="acceptance criterion (repeatable)")
    s.add_argument("--scope", action="append", help="glob the work is confined to (repeatable)")
    s.add_argument("--tag", action="append")
    s.add_argument("--parent")
    s.add_argument("--depends-on", action="append")
    s.add_argument("--priority", type=int, default=50)
    s.add_argument("--model", help="override the model for this card "
                                   "(opus, sonnet, haiku, or a full claude-… id)")
    s.add_argument("--budget", type=float, help="usd ceiling for this card's subtree")
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--start", action="store_true", help="move straight onto stage 1")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("ls", help="list cards")
    s.add_argument("--all", action="store_true", help="include finished")
    s.add_argument("--stage")
    s.add_argument("--type", dest="card_type")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("show", help="everything about one card")
    s.add_argument("id")
    s.add_argument("--full", action="store_true",
                   help="do not clip the brief or the evidence")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("edit", help="change a card")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--brief")
    s.add_argument("--append-brief")
    s.add_argument("--accept", action="append", help="replace acceptance criteria "
                   "(with --add, append instead)")
    s.add_argument("--add", action="store_true", help="add to rather than replace")
    s.add_argument("--scope", action="append")
    s.add_argument("--tag", action="append")
    s.add_argument("--type")
    s.add_argument("--model", help="set the model for this card, or `default` "
                                   "to fall back to the stage and the role")
    s.add_argument("--priority", type=int)
    s.add_argument("--max-attempts", type=int)
    s.add_argument("--requeue", action="store_true",
                   help="clear blocks/defers and put it back in the queue")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("start", help="move backlog cards onto stage 1")
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("link", help="add an edge between cards")
    s.add_argument("src")
    s.add_argument("dst")
    s.add_argument("--kind", default="finish_to_start",
                   choices=["finish_to_start", "artifact", "mutex"])
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("unlink", help="remove an edge between cards")
    s.add_argument("src")
    s.add_argument("dst")
    s.add_argument("--kind", default="finish_to_start",
                   choices=["finish_to_start", "artifact", "mutex"])
    s.set_defaults(fn=cmd_unlink)

    s = sub.add_parser("edges", help="show the edges on the board")
    s.add_argument("id", nargs="?", help="only edges touching this card")
    s.set_defaults(fn=cmd_edges)

    s = sub.add_parser("cancel", help="cancel a card (children are opt-in)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--cascade", action="store_true",
                   help="also cancel every card beneath it")
    s.add_argument("--only", action="store_true",
                   help="cancel just this card, leaving its children alone")
    s.add_argument("--reason", help="why — recorded on the card and in the log")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("blocked", help="why nothing is running")
    s.set_defaults(fn=cmd_blocked)

    s = sub.add_parser("needs", help="open checkpoints waiting on you")
    s.set_defaults(fn=cmd_needs)

    s = sub.add_parser("respond", help="answer a checkpoint")
    s.add_argument("id")
    s.add_argument("response", choices=["approve", "amend", "reject"])
    s.add_argument("--note", help="becomes the next agent's instruction")
    s.add_argument("--as", dest="actor", default="human",
                   choices=["human", "session"],
                   help="who is answering (recorded on the event log)")
    s.set_defaults(fn=cmd_respond)

    s = sub.add_parser("propose", help="(for agents) propose a board change")
    s.add_argument("--from", dest="from_task")
    s.add_argument("--kind", required=True, choices=list(P.KINDS))
    s.add_argument("--title")
    s.add_argument("--brief")
    s.add_argument("--reason")
    s.add_argument("--append")
    s.add_argument("--task")
    s.add_argument("--src")
    s.add_argument("--dst")
    s.add_argument("--gate")
    s.add_argument("--json", help="raw payload JSON, merged last")
    s.add_argument("--accept", action="append",
                   help="acceptance criterion (required for add_task/split)")
    s.add_argument("--scope", action="append")
    s.add_argument("--rationale", default="")
    s.add_argument("--confidence", type=float)
    s.add_argument("--urgency", default="normal", choices=["low", "normal", "high"])
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("proposals", help="proposal history and decisions")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_proposals)

    s = sub.add_parser("workflows", help="show, export or import card-type pipelines")
    s.add_argument("action", nargs="?", default="show",
                   choices=["show", "export", "import"])
    s.add_argument("--file", help="path for import (default .dispatch/workflows.json)")
    s.set_defaults(fn=cmd_workflows)

    s = sub.add_parser("up", help="run the scheduler (and the web board)")
    s.add_argument("-d", "--detach", action="store_true")
    s.add_argument("--no-web", action="store_true")
    s.add_argument("--port", type=int, help="default: a stable port per repo")
    s.add_argument("--host", help="local | tailscale | any | an address")
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("down", help="stop a detached scheduler")
    s.set_defaults(fn=cmd_down)

    s = sub.add_parser("serve", help="web board only, no scheduler")
    s.add_argument("--port", type=int)
    s.add_argument("--host", help="local | tailscale | any | an address")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("tick", help="run the scheduler once, in the foreground")
    s.add_argument("-n", "--count", type=int, default=1)
    s.add_argument("--wait", type=float, default=0,
                   help="seconds to keep ticking while agents finish")
    s.set_defaults(fn=cmd_tick)

    s = sub.add_parser("status", help="one-screen summary")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("log", help="the event log")
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("docs", help="the built-in manual — start with `dispatch docs`")
    s.add_argument("topic", nargs="?",
                   help="topic name; a unique prefix works")
    s.add_argument("--page", type=int, help="page through a long topic")
    s.add_argument("--lines", type=int, default=40, help="lines per page")
    s.add_argument("--search", help="find the topic that mentions a term")
    s.add_argument("--all", action="store_true", help="print the whole manual")
    s.add_argument("--export", metavar="DIR",
                   help="write the manual out as browsable files")
    s.set_defaults(fn=cmd_docs)

    s = sub.add_parser("wait", help="block until cards land (for use from a session)")
    s.add_argument("ids", nargs="*", help="card ids; default: everything unfinished")
    s.add_argument("--tag", help="wait on cards carrying this tag")
    s.add_argument("--type", dest="card_type", help="wait on cards of this type")
    s.add_argument("--timeout", type=float, default=0,
                   help="seconds; 0 waits indefinitely")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--through-checkpoints", action="store_true",
                   help="keep waiting instead of returning when a card needs you")
    s.add_argument("--json", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("channel",
                       help="push board events into a running Claude Code session")
    s.add_argument("--install", action="store_true",
                   help="register it in .mcp.json and print the launch command")
    s.add_argument("--poll", type=float, default=2.0,
                   help="seconds between checks of the board's event log")
    s.set_defaults(fn=cmd_channel)

    s = sub.add_parser("attend",
                       help="block until a decision is yours to make (for a session)")
    s.add_argument("--timeout", type=float, default=480,
                   help="seconds to block before returning 'still working'")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--audience", default="session",
                   choices=["session", "human"])
    s.add_argument("--full", action="store_true",
                   help="do not bound the diff (the reasoning is never bounded)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_attend)

    s = sub.add_parser("hook", help="Claude Code hook endpoints")
    hs = s.add_subparsers(dest="hook_event", required=True)
    hstop = hs.add_parser("stop", help="Stop hook: report the board to a session")
    hstop.add_argument("--block-while-busy", action="store_true",
                       help="keep the session alive while cards are in flight")
    hstop.add_argument("--max-blocks", type=int, default=20,
                       help="give up holding the session open after this many turns")
    hstop.set_defaults(fn=cmd_hook)

    s = sub.add_parser("intent",
                       help="describe what you want; an agent proposes the cards")
    s.add_argument("text", nargs="?", help="the description, or - to read stdin")
    s.add_argument("--title", help="defaults to the first line")
    s.add_argument("--priority", type=int, default=70)
    s.set_defaults(fn=cmd_intent)

    s = sub.add_parser("plan", help="show the plan proposed for a direction card")
    s.add_argument("id")
    s.add_argument("--full", action="store_true",
                   help="do not clip the briefs you are being asked to approve")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("memory", help="what agents have learned about this repo")
    s.add_argument("action", nargs="?", default="ls",
                   choices=["ls", "search", "add", "show", "rm"])
    s.add_argument("title", nargs="?",
                   help="the query for search, the title for add, the id for show/rm")
    s.add_argument("--body", help="the memory itself")
    s.add_argument("--tags", help="comma separated")
    s.add_argument("--kind", default="fact",
                   choices=["fact", "convention", "gotcha", "pointer", "decision"])
    s.add_argument("--source", help="the card that learned it")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("resume", help="clear a pause (including the expansion alarm)")
    s.add_argument("--reset-expansion", action="store_true",
                   help="also restart the expansion window from now")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("upgrade",
                       help="bring an older board's settings up to this version")
    s.add_argument("--apply", action="store_true",
                   help="write the changes (without this it only reports)")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("gc", help="remove worktrees for finished cards")
    s.set_defaults(fn=cmd_gc)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

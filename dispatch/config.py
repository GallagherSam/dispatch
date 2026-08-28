"""Repo-local configuration and the `.dispatch/` layout.

Everything the tool needs lives inside the repo it is orchestrating, so a board
is portable, inspectable, and deletable with `rm -rf .dispatch`.
"""
from __future__ import annotations

import json
import os
from typing import Any

from dispatch import DISPATCH_DIR

# Board columns are a *global* vocabulary.  A card type's workflow is an ordered
# subset of these, which is what keeps the board coherent when several card
# types with different pipelines share one screen.
DEFAULT_STAGES: list[dict[str, Any]] = [
    {"id": "backlog",   "label": "Backlog",   "wip": 0,  "terminal": False},
    {"id": "spec",      "label": "Spec",      "wip": 3,  "terminal": False},
    {"id": "build",     "label": "Build",     "wip": 4,  "terminal": False},
    {"id": "qa",        "label": "QA",        "wip": 4,  "terminal": False},
    {"id": "review",    "label": "Review",    "wip": 4,  "terminal": False},
    {"id": "signoff",   "label": "Needs You", "wip": 0,  "terminal": False},
    {"id": "integrate", "label": "Integrate", "wip": 1,  "terminal": False},
    {"id": "done",      "label": "Done",      "wip": 0,  "terminal": True},
]

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "stages": DEFAULT_STAGES,

    "scheduler": {
        "tick_seconds": 5.0,
        "max_concurrent": 3,
        "lease_seconds": 3600.0,
        "retry_backoff_s": 5.0,     # pause before handing a returned card back
        "heartbeat_seconds": 30.0,
        "paused": False,
    },

    # Containment.  These are the numbers that stop a fleet of agents from
    # talking itself into a four-hundred-card backlog overnight.
    "containment": {
        "max_children_per_parent": 12,
        "max_depth": 3,
        "expansion_ratio_window": 20,
        "expansion_ratio_limit": 2.5,
        "default_budget": {"usd": 25.0, "wall_clock_s": 21600},
        # The default budget is per subtree, so ten cards can spend ten times
        # it without anything objecting. This is the ceiling on the whole board.
        "total_budget_usd": None,
    },

    # Proposals inside these bounds are accepted by deterministic policy and
    # never reach a model.  Everything else climbs the ladder.
    "mutation": {
        "autonomy": "policy",           # policy | arbiter | human
        "auto_accept_kinds": ["add_task", "add_dep", "amend_brief", "raise_blocker"],
        "arbiter_kinds": ["split", "cancel", "request_gate"],
        "duplicate_similarity": 0.82,   # at or above this, merge silently
        "duplicate_review": 0.55,       # between the two, have it judged
    },

    # The agent command is a template, not a hard-coded invocation, because CLI
    # flags move faster than this project will.
    "runner": {
        # Agents run confined to a throwaway git worktree. They still need to
        # run the project's own test and lint commands to check their own work —
        # an agent that cannot run the tests is writing blind and the gate is
        # the first thing that ever sees the truth. `settings.json` grants
        # exactly the commands you configured; set permission_mode to
        # "bypassPermissions" if you would rather not maintain that list.
        "permission_mode": "acceptEdits",
        "command": [
            "claude", "-p",
            "--output-format", "json",
            "--permission-mode", "{permission_mode}",
            "--settings", "{settings_file}",
            "--append-system-prompt-file", "{agent_prompt_file}",
            "--allowedTools", "{allowed_tools}",
            "--model", "{model}",
        ],
        # Which credentials the agents bill against.
        #   subscription  use the claude.ai login (the default). Any
        #                 ANTHROPIC_API_KEY in the environment is removed from
        #                 the agent's environment, because it silently takes
        #                 precedence over the subscription and bills API credits.
        #   api_key       leave the key in place and bill the API.
        #   inherit       pass the environment through untouched.
        "auth": "subscription",
        "timeout_s": 3600,
        "worktrees": True,
        "branch_prefix": "dispatch/",
        # A finished card lands on the base branch. The merge is serialised,
        # rebases first, and re-runs the gates on the rebased tree — a card's
        # tests passing in isolation says nothing about whether they pass on
        # top of whatever landed while it was working.
        "merge_on_done": True,
        "base_branch": None,          # None: whatever the card branched from
        "delete_branch_after_merge": True,
        "merge_retry_s": 30,          # how long to wait when the repo is busy
        # A dirty base tree blocks every card's merge. Say so rather than
        # waiting quietly forever.
        "merge_busy_escalate_after": 10,
        # A card the board calls done must have nothing left on its branch.
        "verify_landed_every_ticks": 20,
    },

    # OS-level confinement for agent processes (Anthropic's sandbox runtime).
    # `diff_scope` catches a stray write after the fact; this stops it in the
    # kernel. Off by default because it needs `srt` installed and a network
    # allowlist -- srt denies network by default and rejects `*`, so there is
    # no filesystem-only mode.
    "sandbox": {
        # auto: confine wherever the OS can, and say so plainly where it
        # cannot. true: refuse to run unconfined. false: off.
        "enabled": "auto",
        # the `--` is load-bearing: srt and the Claude CLI both take
        # `--settings`, and without it srt swallows the agent's
        "command": ["srt", "--settings", "{srt_settings_file}", "--"],
        "allow_write": [],           # extra writable paths beyond the worktree
        "allow_read": [],
        "deny_write": [],
        "deny_read": None,           # None: a sensible default (~/.ssh etc.)
        "allowed_domains": None,     # None: the model API plus registries
        "denied_domains": [],
    },

    "arbiter": {
        "command": ["claude", "-p", "--output-format", "json", "--model", "{model}"],
        "model": "sonnet",
        "timeout_s": 180,
    },

    # host: local | tailscale | any | an explicit address
    # port: a number, or "auto" for a stable per-repo port so several boards
    #       can run at once without colliding
    # An attending Claude Code session holds the context of the larger task, so
    # it is usually better placed than a person to answer "does this diff
    # satisfy what we were doing" — and infinitely faster. Some decisions are
    # still not a session's to make.
    "session": {
        "may_decide": [
            "signoff",           # a human stage: does this work satisfy the card
            "tests_pass", "lint_clean", "build_ok",
            "has_acceptance",    # a session can write the missing criteria
            "diff_scope", "arbiter_judges",
            "merge_conflict", "deadletter", "proposal",
        ],
        # Never a session's call: money, secrets, and runaway detection.
        "human_only": [
            "no_secrets", "budget_remaining", "expansion", "plan",
        ],
    },

    "server": {"host": "local", "port": "auto"},

    # Gates every task runs regardless of workflow.
    "global_gates": {
        "pre_dispatch": ["concurrency", "wip_limit", "mutex_free", "budget_remaining",
                         "has_acceptance", "quota_above:15"],
        "pre_complete": ["diff_scope", "no_secrets", "no_stray_writes"],
    },
}

# Agent types.  `human` is special-cased by the scheduler into a checkpoint.
DEFAULT_AGENTS: dict[str, dict[str, Any]] = {
    "spec": {
        "label": "Spec",
        "model": "sonnet",
        "allowed_tools": "Read,Grep,Glob,Bash(git *),Write,WebSearch,WebFetch",
        "prompt_file": "spec.md",
    },
    "developer": {
        "label": "Developer",
        "model": "sonnet",
        "allowed_tools": "Read,Write,Edit,Grep,Glob,Bash,WebSearch,WebFetch",
        "prompt_file": "developer.md",
    },
    "qa": {
        "label": "QA",
        "model": "sonnet",
        "allowed_tools": "Read,Write,Edit,Grep,Glob,Bash,WebSearch,WebFetch",
        "prompt_file": "qa.md",
    },
    "reviewer": {
        "label": "Reviewer",
        "model": "sonnet",
        "allowed_tools": "Read,Grep,Glob,Bash(git *),WebSearch,WebFetch",
        "prompt_file": "reviewer.md",
    },
    "integrator": {
        "label": "Integrator",
        "model": "sonnet",
        "allowed_tools": "Read,Grep,Glob,Bash",
        "prompt_file": "integrator.md",
    },
    "planner": {
        "label": "Planner",
        "model": "opus",
        "allowed_tools": "Read,Grep,Glob,Bash(git *),WebSearch,WebFetch",
        "prompt_file": "planner.md",
    },
    "human": {"label": "You", "model": None, "allowed_tools": "", "prompt_file": None},
}


def dispatch_dir(root: str) -> str:
    return os.path.join(root, DISPATCH_DIR)


def paths(root: str) -> dict[str, str]:
    d = dispatch_dir(root)
    return {
        "root": d,
        "db": os.path.join(d, "board.db"),
        "config": os.path.join(d, "config.json"),
        "workflows": os.path.join(d, "workflows.json"),
        "agents_json": os.path.join(d, "agents.json"),
        "gates": os.path.join(d, "gates"),
        "agents": os.path.join(d, "agents"),
        "runs": os.path.join(d, "runs"),
        "worktrees": os.path.join(d, "worktrees"),
        "settings": os.path.join(d, "settings.json"),
        "pid": os.path.join(d, "scheduler.pid"),
        "log": os.path.join(d, "scheduler.log"),
    }


def find_root(start: str | None = None) -> str:
    """Walk up looking for `.dispatch/`, then fall back to the git root."""
    cur = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, DISPATCH_DIR)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    cur = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start or os.getcwd())
        cur = parent


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root: str) -> dict[str, Any]:
    p = paths(root)["config"]
    if not os.path.exists(p):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(p) as f:
        return _deep_merge(DEFAULT_CONFIG, json.load(f))


def save_config(root: str, cfg: dict[str, Any]) -> None:
    with open(paths(root)["config"], "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def load_agents(root: str) -> dict[str, dict[str, Any]]:
    p = paths(root)["agents_json"]
    if not os.path.exists(p):
        return json.loads(json.dumps(DEFAULT_AGENTS))
    with open(p) as f:
        return _deep_merge(DEFAULT_AGENTS, json.load(f))


def save_agents(root: str, agents: dict[str, Any]) -> None:
    with open(paths(root)["agents_json"], "w") as f:
        json.dump(agents, f, indent=2)
        f.write("\n")


#: Set any of these and the Claude CLI uses them in preference to the
#: claude.ai login — which is how a fleet of agents quietly bills API credits
#: while you believe it is on your subscription.
API_AUTH_VARS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "AWS_BEARER_TOKEN_BEDROCK",
]


def auth_mode(cfg: dict[str, Any]) -> str:
    return (cfg.get("runner", {}).get("auth") or "subscription").lower()


def agent_environment(cfg: dict[str, Any],
                      base: dict[str, str] | None = None
                      ) -> dict[str, str]:
    """The environment an agent (or the arbiter) is launched with."""
    env = dict(os.environ if base is None else base)
    if auth_mode(cfg) == "subscription":
        for var in API_AUTH_VARS:
            env.pop(var, None)
    return env


def auth_note(cfg: dict[str, Any]) -> str:
    mode = auth_mode(cfg)
    if mode == "subscription":
        overridden = [v for v in API_AUTH_VARS if os.environ.get(v)]
        if overridden:
            return ("claude.ai subscription — " +
                    ", ".join(overridden) + " is set and will be removed from "
                    "the agent environment so it does not bill API credits")
        return "claude.ai subscription"
    if mode == "api_key":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return "api_key — but ANTHROPIC_API_KEY is not set"
        return "api key (billed to the API, not your subscription)"
    return "inherited from the environment"


#: Settings worth changing on a board created by an older version, and why.
#: Only safety-relevant ones — a value you deliberately customised is yours.
RECOMMENDED = [
    {
        "path": "sandbox.enabled",
        "stale": lambda v: v is False,
        "value": "auto",
        "why": ("unconfined, an agent that resolves an absolute path writes "
                "into the repo root instead of its worktree — the card merges "
                "nothing and the dirtied tree blocks every other card"),
    },
    {
        "path": "global_gates.pre_complete",
        "stale": lambda v: isinstance(v, list) and "no_stray_writes" not in v,
        "value": lambda v: [*list(v), "no_stray_writes"],
        "why": "catches a write outside the worktree even when unconfined",
    },
    {
        "path": "runner.auth",
        "stale": lambda v: v is None,
        "value": "subscription",
        "why": ("an ANTHROPIC_API_KEY in the environment silently outranks "
                "your claude.ai login"),
    },
    {
        "path": "mutation.duplicate_review",
        "stale": lambda v: v is None,
        "value": 0.55,
        "why": "an uncertain duplicate gets judged instead of becoming a second card",
    },
]


def dig(cfg: dict[str, Any], path: str, default=None):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def put(cfg: dict[str, Any], path: str, value) -> None:
    node = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def missing_settings(cfg: dict[str, Any]) -> list[str]:
    """Keys this version knows about that an older board never wrote."""
    out = []

    def walk(default, prefix=""):
        for key, val in default.items():
            path = f"{prefix}{key}"
            if dig(cfg, path, _MISSING) is _MISSING:
                out.append(path)
            elif isinstance(val, dict):
                walk(val, path + ".")

    walk(DEFAULT_CONFIG)
    return out


class _Missing:
    pass


_MISSING = _Missing()


def stale_settings(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rec in RECOMMENDED:
        current = dig(cfg, rec["path"], None)
        if rec["stale"](current):
            value = rec["value"]
            out.append({**rec, "current": current,
                        "new": value(current) if callable(value) else value})
    return out


#: Aliases the Claude CLI accepts. Full ids (`claude-opus-5`, dated variants)
#: are accepted too, so this is a hint rather than a gate — models outlive
#: allowlists, and refusing an unknown one would age badly.
MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")


def looks_like_a_model(name: str | None) -> bool:
    if not name:
        return True
    n = str(name).strip().lower()
    return n in MODEL_ALIASES or n.startswith("claude-") or "/" in n or ":" in n


def stage_ids(cfg: dict[str, Any]) -> list[str]:
    return [s["id"] for s in cfg["stages"]]


def stage_def(cfg: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for s in cfg["stages"]:
        if s["id"] == stage_id:
            return s
    return {"id": stage_id, "label": stage_id, "wip": 0, "terminal": False}

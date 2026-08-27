# Config
> `.dispatch/config.json`, annotated.

```jsonc
{
  // Board columns. A card type's pipeline is an ordered subset of these.
  "stages": [
    {"id": "backlog",   "label": "Backlog",   "wip": 0},
    {"id": "build",     "label": "Build",     "wip": 4},
    {"id": "signoff",   "label": "Needs You", "wip": 0},
    {"id": "done",      "label": "Done",      "terminal": true}
  ],

  "scheduler": {
    "tick_seconds": 5.0,
    "max_concurrent": 3,        // agents at once, across the whole board
    "lease_seconds": 3600.0,    // a lapsed lease is reaped and requeued
    "retry_backoff_s": 5.0,     // pause before handing a returned card back
    "paused": false             // stops dispatch and merges; process stays up
  },

  // What stops a fleet talking itself into a four-hundred-card backlog.
  "containment": {
    "max_children_per_parent": 12,
    "max_depth": 3,
    "expansion_ratio_window": 20,
    "expansion_ratio_limit": 2.5,   // created ÷ completed; above this, pause
    "default_budget": {"usd": 25.0, "wall_clock_s": 21600},
    "total_budget_usd": null,       // ceiling for the whole board, not per subtree
    "expansion_ratio_window": 20
  },

  "mutation": {
    "autonomy": "policy",           // policy | arbiter | human
    "auto_accept_kinds": ["add_task", "add_dep", "amend_brief", "raise_blocker"],
    "arbiter_kinds": ["split", "cancel", "request_gate"],
    "duplicate_similarity": 0.82,   // at or above this, merge silently
    "duplicate_review": 0.55        // between the two, have it judged
  },

  // Commands the tests_pass / lint_clean / build_ok gates run.
  "commands": {"test": "pytest -q", "lint": null, "build": null,
               "timeout_s": 900},

  "runner": {
    "auth": "subscription",             // subscription | api_key | inherit
                                        // (see `dispatch docs billing`)
    "permission_mode": "acceptEdits",   // or bypassPermissions
    "command": ["claude", "-p", "--output-format", "json",
                "--permission-mode", "{permission_mode}",
                "--settings", "{settings_file}",
                "--append-system-prompt-file", "{agent_prompt_file}",
                "--allowedTools", "{allowed_tools}", "--model", "{model}"],
    "timeout_s": 3600,
    "worktrees": true,
    "branch_prefix": "dispatch/",
    "merge_on_done": true,
    "base_branch": null,                // null: what the card branched from
    "delete_branch_after_merge": true,
    "merge_retry_s": 30,
    "merge_busy_escalate_after": 10,    // stalled merges ask for help
    "verify_landed_every_ticks": 20     // a card called done must really have landed
  },

  "arbiter": {"command": ["claude", "-p", "--output-format", "json",
                          "--model", "{model}"],
              "model": "sonnet", "timeout_s": 180},

  // host: local | tailscale | any | an address    (see `dispatch docs serving`)
  // port: a number, or "auto" for a stable per-repo port
  // What an attending session may answer for itself.
  "session": {
    "may_decide": ["signoff", "tests_pass", "has_acceptance", "..."],
    "human_only": ["no_secrets", "budget_remaining", "expansion", "plan"]
  },

  "server": {"host": "local", "port": "auto"},

  // OS-level confinement for agents (see `dispatch docs sandbox`).
  "sandbox": {
    "enabled": "auto",          // auto: confine where the OS can
                                // true: refuse to run unconfined
                                // false: off
    "backend": "auto",          // auto | seatbelt | bwrap | srt
                                // auto leaves the internet open; srt does not
    "allow_write": [],          // extra writable paths beyond the worktree
    "allow_read": [],
    "deny_write": [],
    "deny_read": null,          // null = ~/.ssh, ~/.aws, ~/.gnupg, ...
    // srt only:
    "command": ["srt", "--settings", "{srt_settings_file}", "--"],
    "allowed_domains": null,    // null = model API + package registries
    "denied_domains": []
  },

  // Gates every card runs, whatever its pipeline says.
  "global_gates": {
    "pre_dispatch": ["concurrency", "wip_limit", "mutex_free",
                     "budget_remaining", "has_acceptance", "quota_above:15"],
    "pre_complete": ["diff_scope", "no_secrets"]
  }
}
```

The runner command is a template, not a hard-coded invocation — CLI flags move
faster than this project will. Substitutions: `{agent_prompt_file}`
`{allowed_tools}` `{model}` `{permission_mode}` `{settings_file}` `{task_id}`
`{stage}` `{worktree}`.

## Other files

| file | what it is |
|---|---|
| `workflows.json` | card types and their pipelines; commit this |
| `agents.json` | model and tool allowlist per role |
| `agents/*.md` | the prompt for each role — the cheapest quality lever here |
| `settings.json` | what agents may run inside their worktree |
| `gates/*` | your own gates |

`.dispatch/.gitignore` keeps state out of history and leaves configuration in.

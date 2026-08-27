# Sandbox
> Confining agents to their worktree at the OS level, not by asking nicely.

`diff_scope` catches a stray write *after the fact*, by inspecting the diff.
The sandbox stops it in the kernel. Both are worth having; they fail
differently.

**On by default** wherever the OS supports it (`enabled: "auto"`), because
unconfined is genuinely dangerous: an agent that resolves an absolute path
writes into the main repository instead of its worktree, the card then merges
nothing, and the dirtied base tree blocks **every** card from merging — with
the agent reporting success the whole time.

    dispatch init                  # confined, if this OS can
    dispatch init --sandbox        # require it: refuse to run unconfined
    dispatch init --no-sandbox     # off, and the no_stray_writes gate is your
                                   # only net

## Two backends, and the difference is the internet

| backend | filesystem | network | needs |
|---|---|---|---|
| `seatbelt` (macOS) | scoped | **left open** | nothing, ships with macOS |
| `bwrap` (Linux) | scoped | **left open** | `bubblewrap` |
| `srt` | scoped | allow-only | `npm i -g @anthropic-ai/sandbox-runtime` |

`backend: "auto"` (the default) picks `seatbelt` or `bwrap` — the ones that
leave the internet alone, so agents can search, fetch pages, and install
packages normally.

Choose `srt` only if you also want egress locked down:

    dispatch init --sandbox --sandbox-backend srt

**srt blocks web research.** It denies network by default and rejects `*` as an
allowlist entry, so there is no filesystem-only mode. WebSearch still works —
it runs server-side — but `WebFetch` returns `EGRESS_BLOCKED` and `curl` gets
nothing unless the domain is on `sandbox.allowed_domains`.

## What an agent can write

Writes are **allow-only** under every backend, so this list is the entirety of
it:

- its own worktree
- Claude Code's scratch directories, and `$TMPDIR`
- `~/.claude`, `~/.cache`, `~/.npm`
- anything you add to `sandbox.allow_write`

Not on the list, and therefore denied: the repository working tree, `board.db`,
and **every other card's worktree**. Reads of `~/.ssh`, `~/.aws`, `~/.gnupg`,
`~/.config/gh` and `~/.kube` are denied too.

    $ sandbox-exec -f <profile> sh -c 'echo x > .../other-card/src/s.js'
    sh: .../other-card/src/s.js: Operation not permitted

## Configuration

```jsonc
"sandbox": {
  "enabled": "auto",        // auto | true (required) | false
  "backend": "auto",        // auto | seatbelt | bwrap | srt
  "allow_write": [],        // extra writable paths beyond the worktree
  "deny_read": null,        // null = ~/.ssh, ~/.aws, ~/.gnupg, ~/.config/gh, ~/.kube

  // srt only:
  "allowed_domains": null,  // null = model API + package registries
  "denied_domains": []
}
```

`null` means "use the sensible default"; `[]` means "genuinely empty". They are
not the same — an empty `deny_read` denies nothing.

## It never degrades quietly

If the sandbox is enabled and its backend is missing, the scheduler refuses to
start and refuses to dispatch. A security control that silently turns itself
off is worse than none.

## The one way to lose containment

Granting a writable path that *contains* the repository — because the worktrees
live inside it. `dispatch status` flags it:

    sandbox: '/private/tmp/claude-501' is writable and contains this repo —
    agents could reach the board and each other's worktrees through it

In practice this only bites if you keep repos somewhere unusual, like under
`/tmp`.

Next: `dispatch docs serving`

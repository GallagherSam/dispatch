# Security

## What this tool does, plainly

dispatch runs AI coding agents unattended against your repository. Those agents
write files and execute commands. Everything below follows from that.

**Do not point it at a repository you would not let a contractor with shell
access work on**, and do not run it on a machine holding credentials you would
not want a mistake to reach.

## The boundaries that exist

| boundary | what it does | what it does not do |
|---|---|---|
| **Sandbox** (`sandbox.enabled`, default `auto`) | Confines each agent to its own git worktree at the OS level — Seatbelt on macOS, bubblewrap on Linux. Writes outside fail in the kernel. Reads of `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.kube` are denied. | Leaves the network alone by design, so agents can research. Use `backend: "srt"` if you also want egress on an allowlist. |
| **`diff_scope` gate** | Rejects a diff touching files outside the globs a card declared. | After the fact. It is the second net, not the first. |
| **`no_stray_writes` gate** | Catches an agent writing into the repo root instead of its worktree. | Only matters when confinement is off. |
| **`no_secrets` gate** | Escalates when added lines look like credentials. | Pattern matching. It will miss things. |
| **Board HTTP server** | Binds loopback by default and refuses cross-origin writes. | Has **no login**, and the write surface is larger than that sounds — see below. `--host tailscale` puts it on your tailnet, where your ACLs are the only access control. Never expose it publicly. |
| **Budgets and the expansion alarm** | Cap spend per subtree and per board, and pause when the board grows faster than it shrinks. | Advisory against a runaway, not a security control. |

### What the board can do without authenticating

Anyone who can reach the port can create cards, respond to checkpoints and
proposals, pause or resume the scheduler, edit workflows, import a workflow
file, rewrite `config.json` and `agents.json`, and call `dispatch intent` —
which starts a planner run and **spends money**. There is no read-only mode.

The same-origin check stops a web page you visit from driving your board; it
is not authentication, and it does not apply to anything that can make a
direct request. On loopback that boundary is your machine's user account,
which is a reasonable place to draw it for a local tool. On a tailnet it is
your ACLs, and nothing else — so treat `--host tailscale` as granting every
capability above to everyone who can route to that address.

## Things worth knowing before you run it

**Gate scripts execute.** `.dispatch/gates/*` are executables the scheduler
runs, and they are meant to be committed and shared. Cloning a repository and
running `dispatch up` therefore executes that repository's gate scripts — the
same class of trust as `make`, npm lifecycle scripts, or a `Makefile` you did
not read. Read `.dispatch/` before running dispatch on a repository you did not
write.

**Agent output is untrusted input.** Card briefs, run summaries and diffs are
written by models. dispatch treats them as data: the channel pushes only ids and
counts into a session, never agent prose, so untrusted text cannot arrive as an
instruction-shaped event. If you build on top of it, keep that property.

**Credentials.** `runner.auth` defaults to `subscription`, which removes
`ANTHROPIC_API_KEY` and friends from the agent's environment so it uses your
claude.ai login. Agents never see the values either way.

**Linux confinement is less proven than macOS.** The bubblewrap backend is
exercised in CI; Seatbelt has additionally been used in anger. If you are on
Linux and confinement matters to you, verify it yourself before trusting it.

## Reporting a vulnerability

Open a [security advisory](https://github.com/GallagherSam/dispatch/security/advisories/new)
rather than a public issue. This is a personal project, so expect a best-effort
response measured in days, not hours.

Especially interested in: a way for an agent to write outside its worktree with
confinement on, a way for agent-authored text to reach a session as an
instruction, or a way to reach the board's HTTP API cross-origin.

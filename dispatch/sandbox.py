"""OS-level confinement for agent processes.

`diff_scope` catches an agent that wandered outside its card *after the fact*,
by inspecting the diff.  This closes the door instead: a write outside the
card's worktree fails with EPERM in the kernel, not in a gate.  The two are
complementary — keep both.

Two backends, and the difference between them is the network:

  seatbelt / bwrap   filesystem only. The internet stays open, so agents can
                     research, fetch pages, and install packages. This is the
                     default, and what most people want.
  srt                Anthropic's sandbox runtime: filesystem *and* an
                     allow-only network. Stronger, but it denies network by
                     default and rejects `*` as an allowlist entry, so
                     WebFetch and arbitrary HTTP stop working unless the
                     domain is listed. Choose it when you want egress locked
                     down and can maintain the list.

Filesystem writes are allow-only under both, so the write list is the whole of
what an agent can touch.  Notably absent: the repository working tree,
`board.db`, and every other card's worktree.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from typing import Any

BACKENDS = ("auto", "seatbelt", "bwrap", "srt", "none")

#: srt only. The model API, plus what the CLI and common builds need.
DEFAULT_DOMAINS = [
    "api.anthropic.com", "*.anthropic.com",
    "sentry.io", "*.sentry.io",
    "registry.npmjs.org", "*.npmjs.org", "registry.yarnpkg.com",
    "pypi.org", "files.pythonhosted.org",
    "crates.io", "static.crates.io", "index.crates.io",
    "proxy.golang.org", "sum.golang.org",
    "github.com", "*.github.com", "codeload.github.com",
]

DEFAULT_DENY_READ = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.kube"]


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------

def _setting(cfg: dict[str, Any]):
    return cfg.get("sandbox", {}).get("enabled", "auto")


def required(cfg: dict[str, Any]) -> bool:
    """`true` means the board refuses to run without confinement."""
    return _setting(cfg) is True


def enabled(cfg: dict[str, Any]) -> bool:
    """`auto` (the default) confines agents wherever the OS can.

    Unconfined, an agent that resolves an absolute path writes into the main
    repository instead of its worktree: the work never reaches the card's
    branch, and the dirtied tree blocks *every* card from merging. Defaulting
    to off made that the out-of-the-box behaviour, so the default is now to
    confine wherever a backend exists and to say so plainly when one does not.
    """
    setting = _setting(cfg)
    if setting is True:
        return True
    if setting is False or setting is None:
        return False
    return backend_available(resolve_backend(cfg))


def configured_backend(cfg: dict[str, Any]) -> str:
    return (cfg.get("sandbox", {}).get("backend") or "auto").lower()


def resolve_backend(cfg: dict[str, Any]) -> str:
    """`auto` picks the filesystem-only backend for this OS — the one that
    leaves the internet alone."""
    want = configured_backend(cfg)
    if want != "auto":
        return want
    if platform.system() == "Darwin":
        return "seatbelt"
    if platform.system() == "Linux":
        return "bwrap"
    return "none"


def backend_available(backend: str) -> bool:
    return {
        "seatbelt": lambda: platform.system() == "Darwin"
                            and shutil.which("sandbox-exec") is not None,
        "bwrap": lambda: shutil.which("bwrap") is not None,
        "srt": lambda: shutil.which("srt") is not None,
        "none": lambda: True,
    }.get(backend, lambda: False)()


def install_hint(backend: str) -> str:
    return {
        "seatbelt": "sandbox-exec ships with macOS; this is not a macOS host",
        "bwrap": "install bubblewrap (apt install bubblewrap / dnf install bubblewrap)",
        "srt": "npm install -g @anthropic-ai/sandbox-runtime",
    }.get(backend, f"no such sandbox backend '{backend}'")


def restricts_network(backend: str) -> bool:
    return backend == "srt"


# ---------------------------------------------------------------------------
# what an agent may touch
# ---------------------------------------------------------------------------

def claude_scratch_dirs() -> list[str]:
    """Claude Code keeps per-project scratch outside the project. Without these
    its Bash tool fails with EPERM and the agent works blind — the exact
    failure the sandbox exists to prevent us from shipping."""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    out: list[str] = []
    for root in ("/private/tmp", "/tmp"):
        out.append(f"{root}/claude-{uid}")
    tmp = os.environ.get("TMPDIR")
    if tmp:
        out.append(tmp.rstrip("/"))
    if platform.system() == "Darwin":
        out.append("/private/var/folders")
    return out


def resolve(path: str) -> str:
    """Expand `~` and follow symlinks.

    Seatbelt matches on the *real* path, so a rule written against a symlinked
    one never fires. On macOS `/tmp` is a symlink to `/private/tmp`, which means
    a repo anywhere under it left the agent unable to write to its own worktree
    — with the failure showing up as an unexplained permission error inside the
    card rather than as anything about the sandbox.

    Glob patterns are left alone: there is nothing to resolve, and realpath
    would mangle them.
    """
    if "*" in path or "?" in path:
        return os.path.expanduser(path)
    return os.path.realpath(os.path.expanduser(path))


def write_paths(cfg: dict[str, Any], worktree: str) -> list[str]:
    paths = [worktree]
    paths += claude_scratch_dirs()
    paths += ["~/.claude", "~/.cache", "~/.npm", "~/.config/claude"]
    paths += list(cfg.get("sandbox", {}).get("allow_write") or [])
    seen, out = set(), []
    for p in paths:
        p = resolve(p).rstrip("/") or "/"
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _or_default(value: Any, default: list[str]) -> list[str]:
    """`null` means "use the sensible default"; `[]` means "genuinely empty".

    Collapsing those two is how a credential denylist silently becomes no
    denylist at all.
    """
    return list(default) if value is None else list(value)


def read_denies(cfg: dict[str, Any]) -> list[str]:
    return [resolve(p) for p in
            _or_default(cfg.get("sandbox", {}).get("deny_read"), DEFAULT_DENY_READ)]


# ---------------------------------------------------------------------------
# seatbelt (macOS)
# ---------------------------------------------------------------------------

def _sb_quote(path: str) -> str:
    return '"' + resolve(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(cfg: dict[str, Any], worktree: str) -> str:
    """`allow default` keeps the network — and everything else — untouched;
    only the filesystem is narrowed."""
    writes = "\n  ".join(f"(subpath {_sb_quote(p)})" for p in write_paths(cfg, worktree))
    denies = read_denies(cfg)
    deny_block = ""
    if denies:
        subpaths = "\n  ".join(f"(subpath {_sb_quote(p)})" for p in denies
                               if not p.startswith("**"))
        if subpaths:
            deny_block = f"(deny file-read*\n  {subpaths})\n"
    return f"""(version 1)
;; Filesystem scoping only. The network is deliberately left alone so agents
;; can research, fetch pages and install packages.
(allow default)

(deny file-write*)
(allow file-write*
  {writes})

;; the handful of device nodes any shell needs to function
(allow file-write-data
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random")
  (literal "/dev/urandom") (literal "/dev/dtracehelper") (literal "/dev/tty")
  (regex #"^/dev/fd/[0-9]+$") (regex #"^/dev/ttys[0-9]*$"))
(allow file-ioctl (literal "/dev/tty") (regex #"^/dev/ttys[0-9]*$"))

;; the CLI's own cwd bookkeeping file, written beside its scratch dir
(allow file-write*
  (regex #"^/private/tmp/claude-[^/]*-cwd$")
  (regex #"^/tmp/claude-[^/]*-cwd$"))

{deny_block}"""


# ---------------------------------------------------------------------------
# bubblewrap (Linux)
# ---------------------------------------------------------------------------

def bwrap_argv(cfg: dict[str, Any], worktree: str) -> list[str]:
    """Everything read-only, the writable paths bound back in, and the network
    namespace left alone."""
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/run", "--die-with-parent"]
    # The tmpfs over /run hides the host's runtime sockets, and on a systemd
    # box it also erases the target of /etc/resolv.conf, which is a symlink
    # into /run. DNS then fails inside the sandbox while everything else looks
    # fine — the agent has a network namespace and no way to resolve a name,
    # so web research just stops working with no error that says why. Bind the
    # resolver file back, and nothing else from /run.
    resolv = os.path.realpath("/etc/resolv.conf")
    if resolv.startswith("/run/"):
        argv += ["--ro-bind-try", resolv, resolv]
    for p in write_paths(cfg, worktree):
        real = os.path.expanduser(p)
        if os.path.exists(real):
            argv += ["--bind", real, real]
    for p in read_denies(cfg):
        real = os.path.expanduser(p)
        if os.path.exists(real) and not p.startswith("**"):
            argv += ["--tmpfs", real]
    argv += ["--chdir", worktree, "--"]
    return argv


# ---------------------------------------------------------------------------
# srt
# ---------------------------------------------------------------------------

def srt_settings(cfg: dict[str, Any], worktree: str) -> dict[str, Any]:
    sb = cfg.get("sandbox", {})
    return {
        "network": {
            "allowedDomains": _or_default(sb.get("allowed_domains"), DEFAULT_DOMAINS),
            "deniedDomains": _or_default(sb.get("denied_domains"), []),
        },
        "filesystem": {
            "denyRead": [*read_denies(cfg), "**/.env"],
            "allowRead": _or_default(sb.get("allow_read"), []),
            "allowWrite": write_paths(cfg, worktree),
            "denyWrite": _or_default(sb.get("deny_write"), []),
        },
    }


def srt_argv(cfg: dict[str, Any], settings_file: str) -> list[str]:
    """The `--` is load-bearing: both srt and the Claude CLI take a
    `--settings` flag, and without a separator srt's option parser reaches past
    the command name and swallows the agent's."""
    tmpl = cfg.get("sandbox", {}).get(
        "command", ["srt", "--settings", "{srt_settings_file}", "--"])
    argv = [p.replace("{srt_settings_file}", settings_file) for p in tmpl]
    if "--" not in argv:
        argv.append("--")
    return argv


# ---------------------------------------------------------------------------
# the one entry point the runner uses
# ---------------------------------------------------------------------------

def wrap(cfg: dict[str, Any], command: list[str], worktree: str,
         log_dir: str) -> tuple[list[str], dict[str, Any]]:
    """Returns (argv, meta). Raises if the sandbox is on but unusable — a
    security control must never degrade silently."""
    backend = resolve_backend(cfg)
    if backend == "none":
        raise RuntimeError(
            f"sandbox.enabled is true but no backend is available on "
            f"{platform.system()}; set sandbox.backend explicitly or disable it")
    if not backend_available(backend):
        raise RuntimeError(
            f"sandbox.enabled is true but the '{backend}' backend is not "
            f"available — {install_hint(backend)}. dispatch will not quietly "
            f"run agents unsandboxed.")

    meta = {"backend": backend, "network_restricted": restricts_network(backend)}

    if backend == "seatbelt":
        profile = os.path.join(log_dir, "sandbox.sb")
        with open(profile, "w") as f:
            f.write(seatbelt_profile(cfg, worktree))
        meta["profile"] = profile
        return ["sandbox-exec", "-f", profile, *list(command)], meta

    if backend == "bwrap":
        return bwrap_argv(cfg, worktree) + list(command), meta

    settings = os.path.join(log_dir, "srt-settings.json")
    with open(settings, "w") as f:
        json.dump(srt_settings(cfg, worktree), f, indent=2)
        f.write("\n")
    meta["profile"] = settings
    return srt_argv(cfg, settings) + list(command), meta


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def preflight(cfg: dict[str, Any], root: str) -> tuple[bool, list[str]]:
    """(ok, problems). Only an explicit `true` makes an unusable sandbox fatal;
    `auto` degrades to a loud warning so a Linux box without bubblewrap still
    runs."""
    setting = _setting(cfg)
    if setting is False or setting is None:
        return True, [unconfined_warning()]
    backend = resolve_backend(cfg)
    if not backend_available(backend):
        detail = (f"no backend suits {platform.system()}"
                  if backend == "none" else install_hint(backend))
        if required(cfg):
            return False, [f"sandbox.enabled is true but '{backend}' is not "
                           f"available — {detail}"]
        return True, [f"agents are NOT confined: {detail}. "
                      + unconfined_warning()]
    problems = containment_warnings(cfg, root)
    return (not problems), problems


def unconfined_warning() -> str:
    return ("an agent that writes to an absolute path will land work in the "
            "main repo instead of its branch, which blocks every card's merge "
            "— the no_stray_writes gate catches it after the fact")


def describe(cfg: dict[str, Any]) -> str:
    if not enabled(cfg):
        return "off — agents are not confined"
    backend = resolve_backend(cfg)
    if restricts_network(backend):
        return (f"{backend} — worktree-only writes, and egress limited to an "
                f"allowlist (WebFetch and arbitrary HTTP will be blocked)")
    return f"{backend} — worktree-only writes, internet left open"


def containment_warnings(cfg: dict[str, Any], root: str) -> list[str]:
    """Writable regions are allow-only, so the only way to lose containment is
    to grant one that contains the repository itself."""
    if not enabled(cfg):
        return []
    real_root = os.path.realpath(root)
    out = []
    for entry in write_paths(cfg, "<worktree>"):
        if entry == "<worktree>":
            continue
        p = os.path.realpath(os.path.expanduser(entry.split("*")[0]))
        if p in ("/", os.path.expanduser("~")):
            out.append(f"sandbox: '{entry}' is writable and far too broad")
            continue
        if real_root == p or real_root.startswith(p + os.sep):
            out.append(
                f"sandbox: '{entry}' is writable and contains this repo "
                f"({real_root}) — agents could reach the board and each "
                f"other's worktrees through it")
    return out


# kept for callers that only ask "is any sandbox usable?"
def available() -> bool:
    return backend_available("seatbelt") or backend_available("bwrap") \
        or backend_available("srt")

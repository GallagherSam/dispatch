"""The built-in manual.

Docs ship inside the package so `dispatch docs` works from an installed wheel,
anywhere, with or without a board.  Output is plain markdown unless stdout is a
terminal — the main reader here is an agent, and ANSI escapes are noise to it.
"""
from __future__ import annotations

import os
import re
import sys

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

#: reading order, not alphabetical — this is a manual, not a directory
ORDER = ["overview", "setup", "cards", "workflows", "gates", "checkpoints",
         "proposals", "merging", "direction", "memory", "sandbox", "serving",
         "billing", "sessions", "channels", "cli", "config",
         "troubleshooting"]

_ANSI = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_B, _D, _C, _0 = (("\033[1m", "\033[2m", "\033[36m", "\033[0m")
                  if _ANSI else ("", "", "", ""))


def available() -> list[str]:
    if not os.path.isdir(DOCS_DIR):
        return []
    found = sorted(f[:-3] for f in os.listdir(DOCS_DIR) if f.endswith(".md"))
    return [t for t in ORDER if t in found] + [t for t in found if t not in ORDER]


def path_for(topic: str) -> str | None:
    p = os.path.join(DOCS_DIR, topic + ".md")
    return p if os.path.isfile(p) else None


def read(topic: str) -> str | None:
    p = path_for(topic)
    if not p:
        return None
    with open(p) as f:
        return f.read()


def resolve(name: str) -> str | None:
    """Exact match, then unique prefix — `dispatch docs check` finds
    checkpoints."""
    have = available()
    if name in have:
        return name
    hits = [t for t in have if t.startswith(name)]
    if len(hits) == 1:
        return hits[0]
    hits = [t for t in have if name in t]
    return hits[0] if len(hits) == 1 else None


def summary(topic: str) -> tuple[str, str]:
    """(title, one-line summary) from the `# Title` and `> summary` lines."""
    body = read(topic) or ""
    title, note = topic, ""
    for line in body.splitlines():
        if line.startswith("# ") and title == topic:
            title = line[2:].strip()
        elif line.startswith("> "):
            note = line[2:].strip()
            break
    return title, note


def index() -> str:
    out = [f"{_B}dispatch manual{_0}", ""]
    width = max((len(t) for t in available()), default=10)
    for topic in available():
        _, note = summary(topic)
        out.append(f"  {_C}{topic.ljust(width)}{_0}  {note}")
    out += ["",
            f"  {_D}dispatch docs <topic>          read one{_0}",
            f"  {_D}dispatch docs <topic> --page 2 page through a long one{_0}",
            f"  {_D}dispatch docs --search quota   find the topic that says it{_0}",
            f"  {_D}dispatch docs --all            the whole manual{_0}"]
    return "\n".join(out)


def render(body: str) -> str:
    """Light terminal styling. Raw markdown when piped, which is what an agent
    reading this actually wants."""
    if not _ANSI:
        return body
    out, in_code = [], False
    for line in body.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            out.append(f"{_D}{line}{_0}")
            continue
        if in_code:
            out.append(f"{_D}{line}{_0}")
            continue
        if line.startswith("# "):
            out.append(f"{_B}{line[2:]}{_0}")
        elif line.startswith("## "):
            out.append(f"{_B}{line[3:]}{_0}")
        elif line.startswith("> "):
            out.append(f"{_D}{line[2:]}{_0}")
        else:
            line = re.sub(r"`([^`]+)`", _C + r"\1" + _0, line)
            line = re.sub(r"\*\*([^*]+)\*\*", _B + r"\1" + _0, line)
            out.append(line)
    return "\n".join(out)


def paginate(body: str, page: int, per_page: int) -> tuple[str, int, int]:
    lines = body.splitlines()
    total = max(1, -(-len(lines) // per_page))
    page = max(1, min(page, total))
    start = (page - 1) * per_page
    return "\n".join(lines[start:start + per_page]), page, total


def search(term: str) -> list[tuple[str, int, str]]:
    needle, hits = term.lower(), []
    for topic in available():
        for n, line in enumerate((read(topic) or "").splitlines(), 1):
            if needle in line.lower():
                hits.append((topic, n, line.strip()))
    return hits


def whole_manual() -> str:
    chunks = []
    for topic in available():
        chunks.append(f"<!-- dispatch docs {topic} -->\n" + (read(topic) or ""))
    return "\n\n---\n\n".join(chunks)

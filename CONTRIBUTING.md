# Contributing

## Getting set up

```bash
git clone https://github.com/GallagherSam/dispatch
cd dispatch
pipx install --editable .        # `dispatch` now follows your working tree
python3 -m unittest discover -s tests -t .
ruff check .
```

**An editable install follows your working tree**, so a half-finished edit
breaks every `dispatch` command on your machine — including any board you have
running. If you use dispatch while developing it, work in a separate clone.

## What a change needs

- **Tests.** The suite runs the real scheduler, real gates and real git
  worktrees against a throwaway repo; only the model call is replaced by a fake
  agent driven from `.dispatch/fake_agent.json`. Add to that rather than mocking
  internals.
- **A regression test for a bug**, marked `# REGRESSION:` with a sentence on
  what went wrong. Most of the bugs here came from real use, and the comment is
  how the next person knows why the line is shaped that way.
- **`ruff check .` clean.** Two rules are deliberately off because their
  autofixes broke working `sqlite3.Row` access; the reason is in `pyproject.toml`.
- **Python 3.9.** That is the floor, and CI tests it.
- **No new runtime dependencies.** The installed tool is stdlib-only and the
  intent is to keep it that way. Development dependencies are fine.

## Documentation

`dispatch/docs/` is the single source: the pages `dispatch docs <topic>` prints
in the terminal, which is what agents read. `docs/` is generated from it.

```bash
dispatch docs --export docs/
```

Tests fail if the two drift, if a topic is added without exporting, or if the
README starts growing back toward being a manual.

## House style

Comments explain *why*, especially where the shape of the code is a scar. Prose
in the docs is written for someone deciding what to do, not for a spec.

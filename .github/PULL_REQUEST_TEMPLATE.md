## What changed, and why

<!-- The why matters more than the what; the diff already says the what. -->

## How it was verified

<!-- Which tests, and anything you exercised by hand. If it fixes a bug, say
     what the bug did, and add a test marked `# REGRESSION:`. -->

- [ ] `python3 -m unittest discover -s tests -t .` passes
- [ ] `ruff check .` is clean
- [ ] `dispatch docs --export docs/` run, if a doc changed

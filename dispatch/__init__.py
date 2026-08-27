"""dispatch — a board-driven orchestrator for fleets of coding agents.

The design premise: a deterministic scheduler owns the loop and never forms an
opinion about whether the work is finished.  An LLM is called as a subroutine
for judgment calls, and a subroutine cannot halt the loop by returning.
"""

__version__ = "0.1.0"

DISPATCH_DIR = ".dispatch"

#!/usr/bin/env bash
# A gate is an executable. Task JSON arrives on stdin; a verdict goes to stdout.
#
# Environment: DISPATCH_ROOT DISPATCH_TASK_ID DISPATCH_STAGE DISPATCH_HOOK
#              DISPATCH_WORKTREE DISPATCH_DIFF_FILE DISPATCH_BOARD_DB
#
# Four verdicts:
#   pass      proceed
#   defer     not yet — requeue after retry_after_s, no attempt consumed
#   fail      wrong — return to the agent with `evidence`, attempt++
#   escalate  open a human checkpoint
#
# Reference this from a workflow stage by filename (without extension):
#   {"stage": "build", "agent": "developer", "gates": ["example_gate"]}
#
# Exiting 0 with no JSON also counts as a pass, so a one-liner is a valid gate.

cat > /dev/null   # consume the task JSON

if [ -f "$DISPATCH_WORKTREE/CHANGELOG.md" ]; then
  echo '{"verdict":"pass","reason":"changelog present"}'
else
  echo '{"verdict":"fail","reason":"no CHANGELOG.md",
         "evidence":"Add a CHANGELOG.md entry describing this change."}'
fi

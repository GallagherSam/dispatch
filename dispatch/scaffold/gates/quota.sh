#!/usr/bin/env bash
# Print remaining quota as a percentage on stdout. Exit non-zero if unknown.
#
# The `quota_above` gate reads this. Unknown passes — a gate you have not taught
# should not silently stop the board.
#
# Teach it something real, e.g.:
#   ccusage blocks --json | jq -r '100 - .blocks[0].projection.percentOfLimit'
#
exit 1

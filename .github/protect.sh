#!/usr/bin/env bash
# Maintainer-only. Applies the gating described in CONTRIBUTING to this repo.
#
# Branch protection is unavailable on a private repo on the free plan, so this
# has to run *after* the repo is public. It is idempotent — re-run it after
# changing a workflow.
set -euo pipefail

REPO="${1:-GallagherSam/dispatch}"
BRANCH=main

echo "==> $REPO"
vis=$(gh api "repos/$REPO" --jq .visibility)
if [ "$vis" != public ]; then
  echo "repo is $vis — branch protection needs a public repo on the free plan." >&2
  echo "run:  gh repo edit $REPO --visibility public --accept-visibility-change-consequences" >&2
  exit 1
fi

echo "==> merge behaviour: squash only, branch deleted after merge"
gh api -X PATCH "repos/$REPO" \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true \
  -F allow_auto_merge=true \
  --jq '"  squash-only: \(.allow_squash_merge and (.allow_merge_commit|not))"'

echo "==> protecting $BRANCH"
# `ci` is the aggregate gate job in ci.yml. Requiring it rather than the matrix
# jobs means adding a Python version does not silently drop a required check.
gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" --input - <<'JSON' --jq '"  ok"'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["ci"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1,
    "require_last_push_approval": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON

# Fork pull requests: left as a manual step on purpose. The REST endpoint for
# this is not reachable on a private repo, so it could not be verified before
# being written down, and a settings script that half-works is worse than one
# that tells you what it did not do.
#
#   Settings > Actions > General > Fork pull request workflows from
#   outside collaborators  ->  "Require approval for all external contributors"
#
# CI runs on `pull_request`, not `pull_request_target`, so a fork's code never
# sees repository secrets. This is the second net, and it also stops a drive-by
# fork burning Actions minutes.
echo "==> MANUAL: Settings > Actions > require approval for all external contributors"

echo "==> workflow token stays read-only, and cannot approve pull requests"
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -F default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=false

echo "==> release tags are immutable once pushed"
gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null <<'JSON' || \
  echo "  (skipped — a ruleset by this name may already exist; check Settings > Rules)"
{
  "name": "release tags are immutable",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]
}
JSON

echo
echo "==> resulting protection"
gh api "repos/$REPO/branches/$BRANCH/protection" --jq '{
  required_checks: .required_status_checks.contexts,
  up_to_date_required: .required_status_checks.strict,
  approvals: .required_pull_request_reviews.required_approving_review_count,
  code_owner_review: .required_pull_request_reviews.require_code_owner_reviews,
  linear_history: .required_linear_history.enabled,
  conversations_resolved: .required_conversation_resolution.enabled,
  force_push: .allow_force_pushes.enabled,
  deletion: .allow_deletions.enabled,
  applies_to_admins: .enforce_admins.enabled
}'

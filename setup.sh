#!/usr/bin/env bash
# One-time setup for the agent-ops control repo.
# Prereqs: git, gh (GitHub CLI, authenticated), claude (Claude Code CLI)
set -euo pipefail

echo "=== agent-ops setup ==="

# 1. Git init + first commit
if [ ! -d .git ]; then
  git init -b main
  git add .
  git commit -m "chore: initial agent-ops control repo"
  echo "✓ git repo initialized"
else
  echo "✓ git repo already initialized"
fi

# 2. Create the GitHub repo (private) and push
read -rp "Create GitHub repo now? (y/n) " CREATE
if [ "$CREATE" = "y" ]; then
  gh repo create agent-ops --private --source=. --push
  echo "✓ pushed to GitHub"
fi

# 3. Claude subscription token
echo ""
echo "Next: generate a Claude Code OAuth token from your subscription."
echo "Run:   claude setup-token"
echo "Then add it as a secret (org-level if you'll manage multiple repos):"
echo "  gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <you>/agent-ops"
echo ""

# 4. Reminder: labels + branches for each MANAGED repo
cat <<'EOF'
For EACH managed repo, run these once (replace OWNER/REPO):

  # staging branch
  gh api repos/OWNER/REPO/git/refs -f ref="refs/heads/staging" \
    -f sha="$(gh api repos/OWNER/REPO/git/refs/heads/main --jq .object.sha)"

  # labels the pipeline uses
  for L in "triage:done:ededed" "needs-human:d93f0b" "blocked:b60205" \
           "ready-to-merge:0e8a16" "hotfix-ready:d93f0b" \
           "approved-for-agent:1d76db" "found-by-audit:fbca04" \
           "backlog:c5def5" "hotfix-backmerge:5319e7"; do
    NAME="${L%:*}"; COLOR="${L##*:}"
    gh label create "$NAME" --repo OWNER/REPO --color "$COLOR" --force
  done

  # copy the stub workflow
  # cp stubs/managed-repo-triage.yml <repo>/.github/workflows/triage.yml
  # (edit YOUR-USERNAME first)

Then in GitHub UI for each managed repo:
  - Settings → Branches: protect main (require PR + approval + status checks)
  - Settings → Branches: protect staging (require status checks)
  - Settings → General: set default branch to staging
And in agent-ops:
  - Settings → Actions → General → Access: allow reuse from your repos
EOF

echo "=== setup script done ==="

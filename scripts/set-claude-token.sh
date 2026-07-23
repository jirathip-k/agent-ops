#!/usr/bin/env bash
# Run after `claude setup-token` completes. Sets the CLAUDE_CODE_OAUTH_TOKEN
# Actions secret on every repo passed as an argument (or listed one-per-line
# in git-ignored config/local/secret-repos.txt).
# Usage: bash scripts/set-claude-token.sh [owner/repo ...]
set -euo pipefail

cd "$(dirname "$0")/.."

REPOS=("$@")
if [ ${#REPOS[@]} -eq 0 ] && [ -f config/local/secret-repos.txt ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && REPOS+=("$line")
  done < config/local/secret-repos.txt
fi
if [ ${#REPOS[@]} -eq 0 ]; then
  echo "usage: $0 owner/repo [owner/repo ...]  (or fill config/local/secret-repos.txt)" >&2
  exit 1
fi

read -rsp "Paste the token from claude setup-token: " TOKEN
echo

for r in "${REPOS[@]}"; do
  printf %s "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$r"
  echo "✓ secret set on $r"
done
echo "Done. Clear the token from your clipboard/scrollback."

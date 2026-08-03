#!/usr/bin/env bash
#
# Provision target repositories with the three agent-ops lifecycle callers.
#
# Operator tooling. Never invoked by a workflow or an agent; see AGENTS.md.
#
# Reports a plan by default and changes nothing. Pass --apply to act.
#
#   scripts/onboard.sh owner/repo [owner/repo ...]
#   scripts/onboard.sh --apply owner/repo
#
set -euo pipefail

REF="v1"
CONTROL_REPO="jirathip-k/agent-ops"
TEMPLATES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/templates/workflows"
SECRETS=(CLAUDE_CODE_OAUTH_TOKEN AGENT_APP_ID AGENT_APP_PRIVATE_KEY)

apply=false
targets=()
for arg in "$@"; do
  case "$arg" in
    --apply) apply=true ;;
    -h | --help)
      sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
    *) targets+=("$arg") ;;
  esac
done

if [ ${#targets[@]} -eq 0 ]; then
  echo "usage: scripts/onboard.sh [--apply] owner/repo [owner/repo ...]" >&2
  exit 2
fi

if [ ! -d "$TEMPLATES" ]; then
  echo "cannot find templates at $TEMPLATES" >&2
  exit 1
fi

# Stagger schedules per repository so several targets do not open Claude
# sessions on the same minute against one subscription. Derived from the
# repository name so it is stable across runs.
schedule_base() {
  local n
  n=$(printf '%s' "$1" | cksum | cut -d' ' -f1)
  echo $((n % 60))
}

status=0

for repo in "${targets[@]}"; do
  echo "=== $repo ==="

  # The control repository runs the lanes directly, not through callers. Its own
  # workflows reference it (they check out prompts from it), so onboarding it
  # would look like a repo full of legacy callers and delete them.
  if [ "$repo" = "$CONTROL_REPO" ]; then
    echo "  ERROR: $repo is the control repository; it is not a target"
    status=1
    continue
  fi

  if ! meta=$(gh api "repos/$repo" --jq '.default_branch + " " + (.private|tostring)' 2>/dev/null); then
    echo "  ERROR: cannot read $repo — check the name and your access"
    status=1
    continue
  fi
  branch=${meta% *}
  private=${meta#* }
  echo "  default branch : $branch"
  echo "  private        : $private"

  # Secrets: the repository's own, plus the organization secrets this repository
  # is actually allowed to use. Listing the organization's secrets directly would
  # count ones scoped to selected repositories that exclude this one.
  repo_secrets=$(gh secret list --repo "$repo" --json name --jq '.[].name' 2>/dev/null || true)
  org_secrets=$(gh api "repos/$repo/actions/organization-secrets" --jq '.secrets[].name' 2>/dev/null || true)
  missing=()
  for s in "${SECRETS[@]}"; do
    if ! printf '%s\n%s\n' "$repo_secrets" "$org_secrets" | grep -qx "$s"; then
      missing+=("$s")
    fi
  done
  if [ ${#missing[@]} -eq 0 ]; then
    echo "  secrets        : all present"
  else
    echo "  secrets        : MISSING ${missing[*]}"
    status=1
  fi

  # Legacy callers: any workflow pointing at this control repo that is not one
  # of the three current lanes.
  legacy=$(gh api "repos/$repo/contents/.github/workflows?ref=$branch" --jq '.[].name' 2>/dev/null || true)
  drop=()
  while IFS= read -r wf; do
    [ -n "$wf" ] || continue
    case "$wf" in agent-discover-plan.yml | agent-implement.yml | agent-review-release.yml) continue ;; esac
    # Match a reusable-workflow call specifically. A bare mention of the control
    # repository is not enough — workflows legitimately check prompts out of it.
    if gh api "repos/$repo/contents/.github/workflows/$wf?ref=$branch" --jq '.content' 2>/dev/null |
      base64 -d 2>/dev/null | grep -Eq "uses:[[:space:]]*$CONTROL_REPO/\.github/workflows/"; then
      drop+=("$wf")
    fi
  done <<<"$legacy"
  if [ ${#drop[@]} -gt 0 ]; then
    echo "  legacy callers : ${drop[*]}"
  else
    echo "  legacy callers : none"
  fi

  protected=false
  route="direct push"
  if gh api "repos/$repo/branches/$branch/protection" >/dev/null 2>&1; then
    protected=true
    route="pull request"
  fi
  echo "  protected      : $protected -> $route"

  base=$(schedule_base "$repo")
  echo "  schedules      : implement :$base, review :$(((base + 20) % 60)), discover :$(((base + 40) % 60))"

  if [ "$apply" = false ]; then
    echo "  -> dry run; re-run with --apply to make these changes"
    continue
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    echo "  -> refusing to apply while secrets are missing"
    continue
  fi

  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT

  if ! git clone -q --depth 1 --branch "$branch" "https://github.com/$repo.git" "$work/repo"; then
    echo "  ERROR: failed to clone $repo"
    status=1
    rm -rf "$work"
    trap - EXIT
    continue
  fi

  # The rest of the apply for this repository runs as one subshell so that any
  # failing step (a bad clone state, an unmatched sed pattern, a rejected push)
  # exits that subshell instead of the whole script, and is caught below.
  if ! (
    cd "$work/repo"
    git switch -qc ci/agent-ops-"$REF"
    mkdir -p .github/workflows
    for wf in "${drop[@]:-}"; do
      [ -n "$wf" ] && git rm -q ".github/workflows/$wf"
    done

    # sed exits 0 whether or not it substituted anything, so a template whose
    # literal cron or base_branch drifted from these patterns would otherwise
    # render an unstaggered caller and report success. Verify the exact
    # literals the sed patterns below expect are still present in the source
    # templates before trusting the substitution.
    if ! grep -qF 'cron: "17 */6 * * *"' "$TEMPLATES/agent-discover-plan.yml" ||
      ! grep -qF 'cron: "37 * * * *"' "$TEMPLATES/agent-implement.yml" ||
      ! grep -qF 'base_branch: main' "$TEMPLATES/agent-implement.yml" ||
      ! grep -qF 'cron: "7 * * * *"' "$TEMPLATES/agent-review-release.yml"; then
      echo "  ERROR: a template's cron or base_branch literal no longer matches onboard.sh's sed patterns; refusing to render an unstaggered caller" >&2
      exit 1
    fi

    discover_cron="$(((base + 40) % 60)) */6 * * *"
    implement_cron="$base * * * *"
    review_cron="$(((base + 20) % 60)) * * * *"

    sed -e "s|cron: \"17 \\*/6 \\* \\* \\*\"|cron: \"$discover_cron\"|" \
      "$TEMPLATES/agent-discover-plan.yml" > .github/workflows/agent-discover-plan.yml
    sed -e "s|cron: \"37 \\* \\* \\* \\*\"|cron: \"$implement_cron\"|" \
      -e "s|base_branch: main|base_branch: $branch|" \
      "$TEMPLATES/agent-implement.yml" > .github/workflows/agent-implement.yml
    sed -e "s|cron: \"7 \\* \\* \\* \\*\"|cron: \"$review_cron\"|" \
      "$TEMPLATES/agent-review-release.yml" > .github/workflows/agent-review-release.yml

    git add .github/workflows

    if git diff --cached --quiet; then
      echo "  -> already provisioned; no changes needed"
      exit 0
    fi

    git commit -q -m "ci: adopt the agent-ops $REF lifecycle lanes"

    if [ "$protected" = true ]; then
      git push -q -u origin ci/agent-ops-"$REF"
      gh pr create --repo "$repo" --base "$branch" --head ci/agent-ops-"$REF" \
        --title "ci: adopt the agent-ops $REF lifecycle lanes" \
        --body "Adds the three agent-ops \`@$REF\` callers, targeting \`$branch\`. Opened as a pull request because \`$branch\` is protected."
    else
      git push -q origin "HEAD:$branch"
      echo "  -> pushed directly to $branch"
    fi
  ); then
    echo "  ERROR: apply failed for $repo"
    status=1
  fi

  rm -rf "$work"
  trap - EXIT
done

exit "$status"

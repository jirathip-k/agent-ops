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
      if [ "$#" -ne 1 ]; then
        echo "unknown option: $arg" >&2
        exit 2
      fi
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

literal_count() {
  { grep -Fo -- "$2" "$1" || true; } | wc -l
}

# These are deliberately source-template pre-checks. sed reports success when a
# pattern matches nothing (or more than once), so applying is unsafe unless each
# hardcoded input literal occurs exactly once. Dry runs skip this invariant check
# to preserve their existing output and read-only behavior.
if [ "$apply" = true ] &&
  { [ "$(literal_count "$TEMPLATES/agent-discover-plan.yml" 'cron: "17 */6 * * *"')" -ne 1 ] ||
    [ "$(literal_count "$TEMPLATES/agent-implement.yml" 'cron: "37 * * * *"')" -ne 1 ] ||
    [ "$(literal_count "$TEMPLATES/agent-implement.yml" 'base_branch: main')" -ne 1 ] ||
    [ "$(literal_count "$TEMPLATES/agent-review-release.yml" 'cron: "7 * * * *"')" -ne 1 ]; }; then
  echo "ERROR: a template's cron or base_branch literal does not occur exactly once; refusing to apply"
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

  if ! meta=$(gh api "repos/$repo" --jq '.default_branch + " " + (.private|tostring) + " " + .owner.type + " " + .owner.login' 2>/dev/null); then
    echo "  ERROR: cannot read $repo — check the name and your access"
    status=1
    continue
  fi
  read -r branch private owner_type owner <<<"$meta"
  echo "  default branch : $branch"
  echo "  private        : $private"

  # Secrets: the repository's own, plus the organization secrets this repository
  # is actually allowed to use. Listing the organization's secrets directly would
  # count ones scoped to selected repositories that exclude this one. GitHub's
  # repository endpoint does not account for Free-plan private repositories, so
  # discount organization secrets there explicitly.
  #
  # Listing either kind of secret requires admin access on the target, not just
  # push access. A 403 or transient API error must not read as "no secrets" —
  # that would tell an operator to re-create secrets that already exist.
  repo_secrets_status=0
  repo_secrets=$(gh secret list --repo "$repo" --json name --jq '.[].name' 2>/dev/null) || repo_secrets_status=$?
  org_secrets=""
  org_secret_note=""
  org_secrets_status=0
  if [ "$owner_type" = "Organization" ]; then
    if [ "$private" = true ]; then
      org_plan=$(gh api "orgs/$owner" --jq '.plan.name // empty' 2>/dev/null || true)
      if [ -z "$org_plan" ]; then
        org_secret_note="organization plan is unreadable; treating organization secrets as unavailable for this private repository; set these as repository secrets"
      elif [ "$org_plan" = "free" ]; then
        org_secret_note="organization secrets do not reach private repositories on GitHub Free; set these as repository secrets"
      else
        org_secrets=$(gh api "repos/$repo/actions/organization-secrets" --jq '.secrets[].name' 2>/dev/null) || org_secrets_status=$?
      fi
    else
      org_secrets=$(gh api "repos/$repo/actions/organization-secrets" --jq '.secrets[].name' 2>/dev/null) || org_secrets_status=$?
    fi
  else
    org_secret_note="user-owned repositories have no organization secrets; set these as repository secrets"
  fi
  if [ "$org_secrets_status" -ne 0 ]; then
    org_secret_note="organization secrets could not be listed with this credential; listing requires admin access on $owner"
  fi

  secrets_unverified=false
  if [ "$repo_secrets_status" -ne 0 ] || [ "$org_secrets_status" -ne 0 ]; then
    secrets_unverified=true
  fi

  missing=()
  for s in "${SECRETS[@]}"; do
    if ! printf '%s\n%s\n' "$repo_secrets" "$org_secrets" | grep -qx "$s"; then
      missing+=("$s")
    fi
  done
  if [ "$secrets_unverified" = true ]; then
    echo "  secrets        : cannot verify with this credential — listing secrets requires admin access on $repo${org_secret_note:+ — $org_secret_note}"
    status=1
  elif [ ${#missing[@]} -eq 0 ]; then
    echo "  secrets        : all present"
  else
    echo "  secrets        : MISSING ${missing[*]}${org_secret_note:+ — $org_secret_note}"
    status=1
  fi

  # Legacy callers: any workflow pointing at this control repo that is not one
  # of the three current lanes.
  #
  # A failed listing or content fetch must not read as "no legacy callers" —
  # that would leave stale lanes running duplicate schedules against the same
  # subscription, the exact condition this check exists to prevent (the same
  # fail-open pattern #355 fixed for the secrets check above). Only a genuine
  # HTTP 404 (the target has no .github/workflows directory yet) is a real
  # "none"; any other failure is unverifiable and must block apply.
  legacy_unverified=false
  legacy_error=""
  if ! legacy=$(gh api "repos/$repo/contents/.github/workflows?ref=$branch" --jq '.[] | select(.type == "file") | .name' 2>&1); then
    if printf '%s\n' "$legacy" | grep -qE 'HTTP 404|Not Found'; then
      legacy=""
    else
      legacy_unverified=true
      legacy_error="$legacy"
      legacy=""
    fi
  fi
  drop=()
  while IFS= read -r wf; do
    [ -n "$wf" ] || continue
    case "$wf" in agent-discover-plan.yml | agent-implement.yml | agent-review-release.yml) continue ;; esac
    # Match a reusable-workflow call specifically. A bare mention of the control
    # repository is not enough — workflows legitimately check prompts out of it.
    if ! wf_content=$(gh api "repos/$repo/contents/.github/workflows/$wf?ref=$branch" --jq '.content' 2>&1); then
      legacy_unverified=true
      legacy_error="$wf_content"
      continue
    fi
    if printf '%s' "$wf_content" | base64 -d 2>/dev/null | grep -Eq "uses:[[:space:]]*$CONTROL_REPO/\.github/workflows/"; then
      drop+=("$wf")
    fi
  done <<<"$legacy"
  if [ "$legacy_unverified" = true ]; then
    echo "  legacy callers : cannot verify with this credential — $legacy_error"
    if [ ${#drop[@]} -gt 0 ]; then
      echo "  legacy callers : ${drop[*]} (identified before the failure above; there may be more)"
    fi
    status=1
  elif [ ${#drop[@]} -gt 0 ]; then
    echo "  legacy callers : ${drop[*]}"
  else
    echo "  legacy callers : none"
  fi

  # Classic branch protection and rulesets are separate systems with separate
  # APIs; a branch protected only by a ruleset is invisible to the legacy
  # endpoint. Check both, and treat a rules-endpoint failure (for example an
  # older GHES without it) as no active rules rather than aborting the run.
  protected=false
  route="direct push"
  if gh api "repos/$repo/branches/$branch/protection" >/dev/null 2>&1; then
    protected=true
  elif rules=$(gh api "repos/$repo/rules/branches/$branch" --jq 'length' 2>/dev/null) &&
    [ "$rules" -gt 0 ] 2>/dev/null; then
    protected=true
  fi
  if [ "$protected" = true ]; then
    route="pull request"
  fi
  echo "  protected      : $protected -> $route"

  base=$(schedule_base "$repo")
  echo "  schedules      : implement :$base, review :$(((base + 20) % 60)), discover :$(((base + 40) % 60))"

  if [ "$apply" = false ]; then
    echo "  -> dry run; re-run with --apply to make these changes"
    continue
  fi

  if [ "$secrets_unverified" = true ]; then
    echo "  -> refusing to apply; secrets could not be verified"
    continue
  fi

  if [ "$legacy_unverified" = true ]; then
    echo "  -> refusing to apply; legacy callers could not be verified"
    continue
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    echo "  -> refusing to apply while secrets are missing"
    continue
  fi

  if ! work=$(mktemp -d); then
    echo "  ERROR: failed to create temporary worktree for $repo"
    status=1
    continue
  fi
  trap 'rm -rf "$work"' EXIT

  if ! git clone -q --depth 1 --branch "$branch" "https://github.com/$repo.git" "$work/repo"; then
    echo "  ERROR: failed to clone $repo"
    status=1
    rm -rf "$work"
    trap - EXIT
    continue
  fi

  # Temporarily disable the parent's errexit so it can collect the subshell's
  # status. Every fallible apply step is guarded explicitly: Bash suppresses
  # errexit for a compound command used directly as an if/! condition.
  set +e
  (
    cd "$work/repo" || exit 1

    apply_branch=ci/agent-ops-"$REF"
    remote_apply_branch=false
    if [ "$protected" = true ]; then
      if git ls-remote --exit-code --heads origin "refs/heads/$apply_branch" >/dev/null; then
        remote_apply_branch=true
        git fetch -q --depth 1 origin \
          "refs/heads/$apply_branch:refs/remotes/origin/$apply_branch" || exit 1
        git switch -qc "$apply_branch" "origin/$apply_branch" || exit 1
      else
        remote_status=$?
        if [ "$remote_status" -ne 2 ]; then
          echo "  ERROR: failed to inspect the existing onboarding branch for $repo"
          exit 1
        fi
        git switch -qc "$apply_branch" || exit 1
      fi
    else
      git switch -qc "$apply_branch" || exit 1
    fi

    mkdir -p .github/workflows || exit 1
    for wf in "${drop[@]:-}"; do
      if [ -n "$wf" ] && [ -e ".github/workflows/$wf" ]; then
        git rm -q ".github/workflows/$wf" || exit 1
      fi
    done

    discover_cron="$(((base + 40) % 60)) */6 * * *"
    implement_cron="$base * * * *"
    review_cron="$(((base + 20) % 60)) * * * *"
    branch_replacement=${branch//\\/\\\\}
    branch_replacement=${branch_replacement//&/\\&}
    branch_replacement=${branch_replacement//|/\\|}

    sed -e "s|cron: \"17 \\*/6 \\* \\* \\*\"|cron: \"$discover_cron\"|" \
      "$TEMPLATES/agent-discover-plan.yml" > .github/workflows/agent-discover-plan.yml || exit 1
    sed -e "s|cron: \"37 \\* \\* \\* \\*\"|cron: \"$implement_cron\"|" \
      -e "s|base_branch: main|base_branch: $branch_replacement|" \
      "$TEMPLATES/agent-implement.yml" > .github/workflows/agent-implement.yml || exit 1
    sed -e "s|cron: \"7 \\* \\* \\* \\*\"|cron: \"$review_cron\"|" \
      "$TEMPLATES/agent-review-release.yml" > .github/workflows/agent-review-release.yml || exit 1

    # Verify the rendered files as well as the source literals so substitution-
    # side mistakes cannot be committed or pushed.
    if [ "$(literal_count .github/workflows/agent-discover-plan.yml "cron: \"$discover_cron\"")" -ne 1 ] ||
      [ "$(literal_count .github/workflows/agent-implement.yml "cron: \"$implement_cron\"")" -ne 1 ] ||
      [ "$(literal_count .github/workflows/agent-implement.yml "base_branch: $branch")" -ne 1 ] ||
      [ "$(literal_count .github/workflows/agent-review-release.yml "cron: \"$review_cron\"")" -ne 1 ]; then
      echo "  ERROR: rendered callers do not contain exactly one expected cron and base_branch; refusing to apply"
      exit 1
    fi

    git add .github/workflows || exit 1

    if git diff --cached --quiet; then
      if [ "$protected" = true ] && [ "$remote_apply_branch" = true ]; then
        existing_pr=$(gh pr list --repo "$repo" --base "$branch" --head "$apply_branch" \
          --state open --json url --jq '.[0].url // empty') || exit 1
        if [ -z "$existing_pr" ]; then
          # The clone is shallow, so local ancestry is unreliable; ask the
          # compare API whether the branch still has commits the base lacks.
          # A merged (or since-fast-forwarded) branch compares 0 ahead, and
          # GitHub refuses to open a pull request with no commits between refs.
          ahead_by=$(gh api "repos/$repo/compare/$branch...$apply_branch" --jq .ahead_by) || exit 1
          if [ "$ahead_by" -gt 0 ]; then
            gh pr create --repo "$repo" --base "$branch" --head "$apply_branch" \
              --title "ci: adopt the agent-ops $REF lifecycle lanes" \
              --body "Adds the three agent-ops \`@$REF\` callers, targeting \`$branch\`. Opened as a pull request because \`$branch\` is protected." || exit 1
          fi
        fi
      fi
      echo "  -> already provisioned; no changes needed"
      exit 0
    else
      diff_status=$?
      [ "$diff_status" -eq 1 ] || exit 1
    fi

    git commit -q -m "ci: adopt the agent-ops $REF lifecycle lanes" || exit 1

    if [ "$protected" = true ]; then
      git push -q origin "HEAD:$apply_branch" || exit 1
      existing_pr=$(gh pr list --repo "$repo" --base "$branch" --head "$apply_branch" \
        --state open --json url --jq '.[0].url // empty') || exit 1
      if [ -n "$existing_pr" ]; then
        echo "  -> updated $existing_pr"
      else
        gh pr create --repo "$repo" --base "$branch" --head "$apply_branch" \
          --title "ci: adopt the agent-ops $REF lifecycle lanes" \
          --body "Adds the three agent-ops \`@$REF\` callers, targeting \`$branch\`. Opened as a pull request because \`$branch\` is protected." || exit 1
      fi
    else
      git push -q origin "HEAD:$branch" || exit 1
      echo "  -> pushed directly to $branch"
    fi
  )
  apply_status=$?
  set -e

  if [ "$apply_status" -ne 0 ]; then
    echo "  ERROR: apply failed for $repo"
    status=1
  fi

  rm -rf "$work"
  trap - EXIT
done

exit "$status"

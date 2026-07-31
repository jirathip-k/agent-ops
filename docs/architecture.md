# Architecture

## Purpose

agent-ops contains policy and GitHub Actions plumbing for a scheduled,
subscription-backed software agent. It deliberately delegates execution,
isolation, state, CI, and release protection to GitHub instead of rebuilding
them locally.

```text
scheduled caller
      │
      ▼
Discover & Plan ── agent:ready issue
      │
      ▼
Implement ─────── agent/issue-... branch ── draft PR
      │
      ▼
Review & Release ── agent:approved
      │
      ▼
branch protection + human merge
```

Each target repository owns its schedule, secrets, permissions, issues,
branches, pull requests, and CI. The public agent-ops repository supplies the
called workflow and prompt.

## Lanes

### Discover & Plan

This combines discovery, triage, grooming, specification, and planning.
Those activities answer one question: is there a small, evidence-backed task
that an implementation agent can execute without another decision?

The target caller deterministically labels new issues from owners, members,
and collaborators as `agent:needs-plan`; this intake job does not invoke a
model. The lane triages those queued issues first, then discovers new work up
to its cap. It can write issues and labels but cannot write repository
contents.

### Implement

This lane selects one oldest `agent:ready` issue and claims it. Claude Code
Action prepares an `agent/issue-...` branch and commits the implementation.
The deterministic workflow step opens a draft PR. The source branch is the
remote equivalent of the old worktree isolation; the Actions runner itself
is already an ephemeral checkout, so another local worktree adds nothing.

A custom GitHub App token is minted for only the target repository. Its
identity allows the branch and PR events to start the target's normal CI.
The lane rejects changes under `.github/workflows/` and never merges.

### Review & Release

This lane selects one PR labeled `agent:review` and starts a fresh read-only
agent session. It reads the issue, diff, and CI results, posts one verdict,
and applies either `agent:approved` or `agent:changes-requested`.

“Release” means declaring readiness for the repository's existing protected
merge path. The workflow does not merge, deploy, or promote.

## Trust boundaries

- The OAuth token authenticates model usage against the owner's Claude
  subscription.
- The workflow `GITHUB_TOKEN` handles issue and review metadata.
- The custom GitHub App token handles implementation branches and PRs.
- The model never receives authority from issue, PR, comment, code, or CI-log
  text.
- Branch protection and a human own the final merge.
- Target CI remains authoritative; the agent does not reimplement its gates.

The implementation agent receives edit tools and a bounded set of common
project commands, but no general GitHub-write or git-history commands.
Workflow-file changes are also rejected deterministically after the run.
The independent reviewer receives no file-editing tools.

## Provider boundary

Model choice is a workflow input:

```text
discover-plan.model
implement.model
review-release.model
```

These currently name Claude subscription models or aliases. There is no
runtime protocol or adapter layer. When an official subscription-authenticated
Codex GitHub Action becomes available, replacing the implementation action
does not affect issue state, branch naming, review, or release policy.

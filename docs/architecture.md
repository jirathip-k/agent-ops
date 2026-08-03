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
Action runs in agent mode and only edits the working tree; it creates no
branch and no commit. Deterministic workflow steps then stage the result on
an `agent/issue-...` branch, gate it, commit, push, and open a draft PR. The
source branch is the remote equivalent of the old worktree isolation; the
Actions runner itself is already an ephemeral checkout, so another local
worktree adds nothing.

A custom GitHub App token is minted for only the target repository. Its
identity allows deterministic workflow steps to create branches and PRs whose
events start the target's normal CI. The lane rejects changes under
`.github/workflows/` and `.github/actions/`, and never merges.

### Review & Release

This lane selects one PR labeled `agent:review` and starts a fresh read-only
agent session. It reads the issue, diff, and CI results, posts one verdict,
and applies either `agent:approved` or `agent:changes-requested`.

A deterministic step then takes an approved pull request out of draft. It
reads the label the reviewer applied rather than asking the model, so a
`REQUEST CHANGES` verdict cannot lift the draft.

“Release” means declaring readiness for the repository's existing protected
merge path. Lifting the draft is part of declaring that readiness; the
workflow does not merge, deploy, or promote, and branch protection and a
human still own the merge.

## Trust boundaries

- The implementation runner is trusted with the Claude subscription token.
  The action places that token in the agent's environment for model
  authentication, so it cannot be withheld from the agent.
- The workflow `GITHUB_TOKEN` handles issue and review metadata.
- The custom GitHub App token handles implementation branches and PRs. It is
  also reachable by the implementation agent: `claude-code-action` assigns its
  `github_token` input to `GH_TOKEN` in the agent's own process environment,
  and the checkout persists it in git credentials. Removing the token from the
  workflow step's `env:` does not withhold it. The implementation runner is
  therefore trusted with both tokens.
- The model never receives authority from issue, PR, comment, code, or CI-log
  text.
- Branch protection and a human own the final merge.
- Target CI remains authoritative; the agent does not reimplement its gates.

The implementation agent receives edit tools and an allowlist of common
project commands. Entries such as `npx`, `python`, and `make` are general
shell escapes, so this allowlist is a guardrail against casual scope creep,
not a security boundary. `gh` is not on the allowlist and the issue text is
supplied as a file, but neither fact isolates the agent from the tokens above.
Changes under `.github/workflows/` and `.github/actions/` are rejected
deterministically after the run and before anything is pushed, against both
the staged tree and any commit a project command made on its own.
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

# ADR 0006: A hand-started session claims its own issue

**Status:** accepted · 2026-07-27

## Context

`agent implement`, `agent resume` and `agent spawn` all claim their issue
(`claims.claim`) and release it when the run ends. The one path that never
ran any of that code was also the platform's most common: an agent started
by hand in a worktree created outside agent-ops (an Orca worktree/terminal,
or a plain `git worktree add`). `agent claim` existed for exactly that case,
but as a *second*, separately-remembered command — so the failure mode was
silent omission, not an error, and nothing surfaced it except `agent doctor`,
itself another command someone has to think to run.

sendmeter #269 is the worked example: a local agent was dispatched into a
worktree, nothing claimed the issue, and the scheduled CI triage lane picked
up the same open, unclaimed bug thirteen minutes before the local agent
pushed its PR. Both PRs implemented the same fix; one was closed unmerged.
The orchestrator's existing open-PR/branch-name guard (`prompts/
orchestrator.md`) cannot close this window — between dispatch and the local
agent's first push, neither an open PR nor a pushed branch exists yet, and
that window is exactly as long as the implementation.

Note while investigating: `claims.audit()` already computes `unclaimed` —
issues with a local `fix/issue-N` worktree and no claim label — and
`agent doctor` already reports it with a remediation command. That landed in
#134, before this issue was filed, so the acceptance criterion "`agent
doctor` reports issues worked locally with no claim" was already met; it did
not fix the root problem because it is itself a separate command nobody is
prompted to run.

## Decision

Make the claim a consequence of starting a session, not a parallel
obligation (direction 1 from the issue), for the one runtime that can carry
it: Claude Code has a `SessionStart` hook, and `agent init` now seeds one.

- `agent claim`'s `issue` argument becomes optional, and gains `--auto`:
  derive the issue from the current branch (`git symbolic-ref`, parsed by
  the existing `fix/issue-N` convention in `runs.issue_from_branch`) instead
  of taking one on the command line.
- `--auto` is built for a hook, not a human: every failure mode — branch
  doesn't name an issue, no `git`, no `gh`, no `origin` remote, the API call
  itself failing — is a silent exit 0, never an error. A hook has no one to
  show an error to, and a spurious failure there must never block a session
  from starting.
- `agent init` writes `.claude/settings.json` (checked in, unlike the
  runtimes' own `.claude/settings.local.json`) with a `SessionStart` hook
  running `agent claim --auto`. Because it is checked in, a worktree created
  for `fix/issue-N` off a branch that already has it carries the hook before
  any agent-ops command has ever run there — the hand-started case this
  issue is about.
- No matching `SessionEnd`/release hook. `agent implement`'s attempts and
  review each run their own headless Claude Code session; a release hook
  would fire when one of those inner sessions ends and drop the outer run's
  claim mid-attempt — a regression, not a fix. Releases stay exactly where
  they are (`implement`/`spawn`/`report`, plus the TTL for anything that
  crashes), and re-claiming an already-labeled issue is a no-op, so the hook
  firing harmlessly inside a dispatched run (which already claimed) is fine.

## Consequences

- Starting a Claude Code session on a `fix/issue-N` branch claims the issue
  without a remembered second step, closing the sendmeter #269 window for
  any repo that re-runs `agent init` (or gets the seeded file some other
  way).
- Degrades safely to exactly today's behavior in every case it doesn't
  cover: a declined hook approval, a non-Claude runtime, `gh` unavailable,
  or a branch that isn't `fix/issue-N`. None of these are new failure modes;
  they are the status quo `agent claim` already had.
- A managed repo only gets the hook after `agent init` runs again there — it
  is not retroactive. Called out as a follow-up in the PR rather than pushed
  as an automated change to those repos.
- `prompts/orchestrator.md` and `.github/workflows/` are untouched — the
  lane side of the race is a separate, human-reviewed change if it's ever
  pursued, not part of this fix.

## Rejected alternatives

- **Narrow the window from the lane's side** (direction 3): the CI lane
  cannot see an unpushed local worktree from a fresh runner, so there is
  nothing to check against before the first push — the exact window that
  matters is invisible from there.
- **Detect-only, no claim** (direction 2): `agent doctor` already does this
  (#134) and it did not prevent #269, because it is itself an unprompted,
  separately-run command. Worth keeping as a backstop, not sufficient alone.
- **A thin `agent dispatch`-style wrapper around raw worktree creation**:
  rejected because it re-introduces the same gap for anyone who creates the
  worktree by hand or through Orca directly, which is the common path this
  issue is about — the session-start hook covers that path and the wrapped
  one alike.

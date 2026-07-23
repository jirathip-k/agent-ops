---
name: agent-ops
description: Operate the agent-ops CLI (agent) to groom, plan, implement, and review GitHub issues through gated agent workflows. Use when the user asks to implement an issue, review a PR, check the agent queue, plan a fix, onboard a repo for agents, or inspect/clean agent worktrees.
---

# Operating the agent-ops CLI

The `agent` CLI (installed globally, editable from ~/Projects/agent-ops)
orchestrates coding agents through: worktree → plan (fable, read-only) →
implement loop (sonnet, gated by the project's test/lint/typecheck commands)
→ self-review (fable, read-only) → commit → PR. You are the operator: run
the commands, interpret results, and report — the pipeline's own agents do
the coding.

## Commands

```sh
agent queue                        # open agent-ready issues, oldest first
agent plan <N> [--post]            # planner only; --post comments the plan on the issue
agent implement <N> [--no-pr] [--keep-worktree]
agent review <PR> [--post]         # read-only PR review; --post comments on the PR
agent worktree list | remove <task-id> [--force]
agent init                         # onboard a repo (AGENTS.md + CLAUDE.md link + .agent/)
agent doctor                       # verify CLIs + config + gates
```

All commands accept `--project <path>`; default is the current directory.
Run them from the target project's root, not from agent-ops.

## Operating rules

1. **Trust ladder.** First runs in any project: `--no-pr --keep-worktree`,
   then show the user the diff (`git -C .worktrees/issue-<N> diff HEAD` or
   `log -p`) before anything is pushed. Only drop `--no-pr` once the user
   has approved earlier results in that project.
2. **Grooming gate.** Never label an issue `agent-ready` yourself unless the
   user asked; an issue qualifies only with acceptance criteria, small
   scope, and no danger zones (see the project's AGENTS.md).
3. **Never merge.** PRs are merged by the user. Posting reviews/plans as
   comments (`--post`) is fine when asked.
4. **Failures keep the worktree.** If a run fails (gates exhausted, planner
   ESCALATE, review REQUEST CHANGES), inspect the kept worktree and the
   printed gate output, summarize the root cause for the user, and propose:
   retry, fix the issue description, or take over manually. Clean up with
   `agent worktree remove issue-<N> --force` only after reporting.
5. **A worktree already exists** for a task → a previous run is in flight or
   left over; check `agent worktree list` and ask/inspect before removing.
6. **Parallel runs** are safe (worktree per task) — start each
   `agent implement` in its own background shell or Herdr pane.

## Configuration facts

- Project config: `.agent/config.yaml` (merged over platform
  `config/defaults.yaml`). Gates come from `commands.test/lint/typecheck` —
  a project without them has NO safety net; run `agent doctor` first.
- Model tiers: `model_tiers.claude_code: {smart: fable, fast: sonnet}`;
  roles reference tiers under `agents:`. Change tiers, not roles, to
  upgrade models.
- `runtime.stream: true` prints live agent activity; disable for quiet logs.
- Retries are fresh-context: original task + gate-failure report, max
  `loop.max_attempts` (default 3).

## Interpreting a run

Stage log lines to relay to the user: `plan ready (N lines)` /
`Planner escalated: …` (stopped, nothing changed) / `attempt K/N` /
`gates failed: test, lint` / `self-review verdict: APPROVE|REQUEST CHANGES`
/ `opened PR: <url>`. A successful `--no-pr` run leaves the commit on branch
`fix/issue-<N>`; push later with
`git -C .worktrees/issue-<N> push -u origin fix/issue-<N>` + `gh pr create`,
or rerun without `--no-pr` after removing the worktree and branch.

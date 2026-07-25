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
agent board sync                   # push open issues from config/board.yml repos to the Projects board
agent merge <PR> [--override]      # squash-merge into staging IF rules pass (CI, caps, blocked paths)
agent promote                      # open the staging → main changelog PR (human merges it)
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
3. **Branch model.** Projects with `base_branch: staging` use the promotion
   flow: agent PRs target staging; `agent merge` may merge them when its
   rules pass (CI green, diff caps, no blocked paths). `--override` is a
   human decision — use it only when the user explicitly authorized the
   specific violation (e.g. an approved auth change), and fix what can be
   fixed (lockfile drift, size) instead of overriding it. The stable branch
   (`merge.stable_branch`, usually main) is HUMAN-ONLY: `agent promote`
   opens the verification PR, the user merges it. Never merge to main.
4. **Failures keep the worktree.** If a run fails (gates exhausted, planner
   ESCALATE, review REQUEST CHANGES), inspect the kept worktree and the
   printed gate output, summarize the root cause for the user, and propose:
   retry, fix the issue description, or take over manually. Clean up with
   `agent worktree remove issue-<N> --force` only after reporting.
5. **A worktree already exists** for a task → a previous run is in flight or
   left over; check `agent worktree list` and ask/inspect before removing.
6. **Parallel runs** are safe (worktree per task) — dispatch each one; they
   get separate worktrees and branches. But two issues that touch the same
   files will conflict on merge, and `.pbxproj` merges especially badly:
   sequence those instead of racing them.
7. **Check labels before dispatching.** `needs-human` / `blocked` mean the
   grooming gate already decided this isn't agent-work; dispatching anyway
   just burns a planner run on an ESCALATE. Note the CI grooming lane may add
   those labels *after* you file an issue, so re-read them at dispatch time.
8. **The planner reads the issue thread** (#29, fixed in #52). An approved
   `## Agent spec` / `## Agent plan` comment reaches it — pinned even when the
   thread is long enough to push it past the recent-comment window. So a
   posted spec no longer forces a workaround. `--plan-file` is still the
   stronger option when you want the plan used *verbatim*: comments inform the
   planner, a plan file replaces it.
9. **Write plans from verified facts, not assumed ones.** A wrong claim about
   the data model in a plan file becomes a wrong implementation that passes
   its gates — self-review catches it, but each round costs a full run. Read
   the type/field/call site first; cite `file:line` in the plan so the
   implementer and reviewer can both check you.
10. **A self-review REQUEST CHANGES is signal, not noise.** Read the findings
   before re-running, and verify the blocking ones yourself — they are as
   often defects in the *plan* as in the code, and re-running an unchanged
   plan reproduces them exactly.
11. **The dedupe guard has a false positive** (#64): any PR whose body merely
   mentions `#N` blocks issue N. Confirm the PR actually closes the issue
   before believing the message; `--force` overrides.

## Onboarding a new project (repeatable procedure)

When the user asks to set up a project for agents, do all of these:

1. **Inspect first**: `git status` (note dirty files / current branch),
   remote URL (repo may live in an org, not the user account), default
   branch, and the test/lint/typecheck commands (`package.json` scripts,
   `pyproject.toml`, Makefile). Note which are missing.
2. `agent init --project <path>`.
3. **Instruction-file direction**: if the repo already has a real CLAUDE.md,
   delete the generated AGENTS.md template and symlink `AGENTS.md ->
   CLAUDE.md` (the existing file stays canonical). Repos with neither keep
   the default direction (AGENTS.md canonical, CLAUDE.md symlink).
4. Fill `.agent/config.yaml`: `base_branch` from the repo's default branch,
   real gate commands with a comment noting what each maps to, and a comment
   flagging any missing gate (e.g. "no test script yet").
5. `gh label create agent-ready --repo <owner>/<repo> --color 1d76db
   --description "Groomed and safe for an agent to implement" --force`.
6. Add the repo to `config/board.yml` `repos:` in the agent-ops checkout,
   then `agent board sync`.
7. `agent doctor --project <path>` must pass; relay any skipped-gate warning
   to the user as a risk (no test gate = no safety net).
7b. If the repo deploys a frontend, check the preview-environment standard
   (docs/workflow.md): PR previews exist, previews use the DEV backend,
   auth allow-list has the origin-only preview wildcard, prod deploys only
   from main. File an issue for any gap instead of fixing inline.
8. Leave the new files **uncommitted** in the project repo for the user to
   review, but commit the board.yml change in agent-ops.

## Spawning runs on a visible surface

**Always use `agent dispatch <N> --project <path>`. Never run `agent
implement` in a plain background shell.** Dispatch picks the most visible
available surface automatically:

- **orca** (if the Orca app is running): a named terminal attached to the
  issue's worktree card, so the run is visible and survives your session.
  Read it with `orca terminal read --terminal <handle> --limit <n>`.
- **background** (last-resort fallback): detached process logging to
  `<project>/.agent-runs/agent-issue-<N>.log`.

Force one with `--surface orca|background`. New surfaces are one class
in `src/agent_ops/surfaces.py`.

**`dispatch` forwards `--plan-file`**, so an approved plan is never a reason
to drop to a raw shell. Use it whenever the spec lives outside the issue body
(see the grooming note below).

Two traps, both learned the hard way:

- A `nohup ... &` shell inside a Claude session is invisible to Orca **and**
  detaches from the harness, so the assistant never receives a completion
  signal — the wrapper exits immediately and the run finishes silently. Poll
  `orca terminal read` instead of inventing your own background wrapper.
- `dispatch` pre-creates the worktree from `origin/<base>`. Dispatching an
  issue whose work depends on an unmerged PR gives the agent a tree without
  that PR's files. Merge the dependency first, or expect a wasted run.

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

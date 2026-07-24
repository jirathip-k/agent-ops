# Day-to-day workflow

How work actually flows through the platform: capture → groom → spec →
dispatch → review → merge, with Orca as the aggregate view and the
cockpit for parallel runs.

## 0. One-time per project

```sh
cd ~/Projects/your-app
agent init        # AGENTS.md (canonical) + CLAUDE.md symlink + .agent/config.yaml
agent doctor
```

`AGENTS.md` is the canonical instruction file; `CLAUDE.md` is a symlink to it
so Claude Code, Codex, and any other runtime read the same project knowledge.
If a repo already has a real `CLAUDE.md`, `init` leaves it alone — either keep
both in sync manually or replace one with a symlink yourself.

## 1. Capture — everything becomes a GitHub issue

Ideas, bugs, chores — file them where you are (`gh issue create`, GitHub
mobile, a Claude session). An issue is the unit of agent work; if it isn't an
issue, the agents can't see it. `agent init` installs an issue template that
nudges checklist acceptance criteria at capture time — for UI work, one
checkbox per affected surface. One caveat on capture channels: GitHub's API
can't attach images, so file UI issues with screenshots from the browser or
mobile, not from a CLI/agent session.

You don't have to be the only source of ideas:

```sh
agent scout --project <app>       # or --max N (default 3)
```

runs a read-only discovery agent that mines signals already in the repo —
TODO/FIXME comments, merged-PR review threads that deferred work
("follow-up", "out of scope"), error paths that swallow failures, untested
modules — and files at most N issues labeled `backlog` + `proposed-by-agent`,
each citing its signal (file:line or PR link). It never brainstorms from a
blank page and never fixes anything; filed issues enter the same groom/spec
funnel as yours. Run it weekly-ish, or whenever the queue runs dry.

## 2. Groom — decide what an agent may do

An issue is agent-ready when it has acceptance criteria, is small enough to
verify (roughly ≤ half a day of human work), and touches no danger zone from
`AGENTS.md`. You can label by hand:

```sh
gh issue edit 123 --add-label agent-ready
```

…but you don't have to. `agent triage` buckets new issues (and now re-checks
issues the CI lane stamped `triage:done` without a bucket), and:

```sh
agent groom --project <app>
```

re-validates *every* open issue against the working branch: closes ones whose
fix is already verifiably in the code (checked by file content, immune to the
squash-promotion ancestry trap), closes duplicates/obsolete ones, promotes
workable issues to `agent-ready` (writing a one-line acceptance criterion into
the groom comment when missing), and refreshes stale buckets. Run it when you
sit down to work; every action lands as a labeled comment on the issue, so
it's auditable and reversible (reopen / relabel).

**UI-facing issues have a higher bar**: triage and groom only promote them
when checklist acceptance criteria name each affected surface/screen. A
one-line criterion isn't enough there — missed surfaces (the modal's other
step, the alternate grouping, the expanded row) are the top cause of
reopened issues.

### 2b. Spec — turn a parked idea into agent-ready work

`backlog` used to be a dead end: ideas sat until you wrote acceptance
criteria yourself. Now:

```sh
agent spec 123                    # explores code, posts the spec as a comment
agent spec 123 --no-post          # print only
gh issue edit 123 --add-label agent-ready --remove-label backlog
```

A read-only agent (smart model) reads the issue *and its comments*, walks
the code to enumerate every surface the request touches, and posts a spec
comment: checklist acceptance criteria (one box per surface/behavior),
affected files, S/M/L size (L comes with a proposed split), and open
questions. If the idea needs a product/data/security decision it escalates
instead of guessing. Your job shrinks to reading the spec and flipping the
label — the spec comment becomes the source of truth for the planner and
implementer.

Grooming also runs in CI (`stubs/managed-repo-groom.yml`, daily): the same
`agent groom` code path executed in Actions, so verdicts can't drift between
lanes. Know what that closes: since the CI triage lane treats `agent-ready`
as its go-ahead, a CI groom promotion feeds the next triage tick — filed →
groomed → implemented → auto-merged to staging, with no human touch until
promotion. That's the intended autonomy level (decided 2026-07-23); the
guardrails are the merge caps, blocked paths, the tester/reviewer gates, and
humans owning `main`.

In the local lane the human gate is still **dispatch and merge** — nothing
runs without `agent implement`, nothing lands without your merge. In the CI
lane the go-ahead label *is* dispatch, so the human gates are grooming
oversight (relabel/reopen) and promotion. `approved-for-agent` remains a
human-only label (see `prompts/orchestrator.md`).

## 3. Dispatch — run the loop

```sh
agent queue                 # open agent-ready issues, oldest first
agent implement 123         # worktree → loop → gates → self-review → PR
```

Parallelism is free because every task gets its own worktree — run several
`agent implement` commands at once (see Orca below). While building trust in
a new project, use `--no-pr` and inspect the kept worktree before pushing.

## 4. Review & merge — humans own main

```sh
agent review 45 --post      # agent pre-review as a PR comment
gh pr checkout 45           # your own look
gh pr merge 45 --squash
```

The agent review is a pre-filter, never the approval. Merge is always yours.

## 5. Background — the CI lane

The scheduled triage pipeline handles the long tail (triage, small fixes,
audit issues) across repos registered in `config/repos.yml`. Check its run
summaries and `needs-human` labels once a day; that's your ops inbox.

## Preview-environment standard (deployed frontends)

Every managed repo that deploys a frontend should meet four rules:

1. **PR previews exist** — promotion PRs build an ephemeral preview
   (Azure SWA: `pull_request` trigger, auto-deleted by the close job when
   the PR merges; Vercel: preview deployments).
2. **Previews use the DEV backend, production uses prod** — via GitHub
   environments (`preview` / `production`) selected by event type, or the
   platform's native per-environment variables (Vercel). Clicking around a
   preview must never touch production data.
3. **Auth redirect allow-lists include the preview wildcard** — and note
   the gotcha: apps redirecting to `window.location.origin` need the
   **origin-only** pattern (no trailing `/**`); the `/**` variant only
   matches URLs that have a path and never matches a bare origin.
4. **Production deploys only from `main`** — never from staging or task
   branches.

When onboarding a repo, check these and file an issue for any gap.

## Public repo, private registries

This repo is public; the names of the repos it manages are not. The split:

- `config/local/` is **git-ignored** and holds the real registries — currently
  `repos.yml` (the repo list `agent status` reads). Copy the committed
  `config/repos.example.yml` there and fill it in on a new machine.
- The CI lane has no central registry at all: each managed repo carries its
  own stub workflow and passes its settings as workflow inputs, so managed
  repo names only ever appear inside the managed repos themselves.
- History was scrubbed (git-filter-repo) before the repo went public, so old
  revisions of these files are gone from every branch.

## The aggregate view

Orca IDE aggregates issues and PRs across repos, so there is no separate
board to maintain. Agents key off labels only — visible in `gh issue view`
and surviving in the issue itself.

## Orca (worktree cockpit, running agents in parallel)

[Orca](https://www.onorca.dev/) is a desktop IDE built around parallel
agents in git worktrees — worktree list, diff viewer, PR/CI inspection, and
terminals in one window. It overlaps heavily with what this platform already
does, so the rule is: **agent-ops orchestrates, Orca observes.**

- Open the **main checkout** in Orca. Task worktrees appear under
  `.worktrees/` as `agent implement` creates them — turn on the repo's
  "show external worktrees" setting so Orca displays worktrees it didn't
  create itself.
- `agent dispatch <N>` spawns each run in an Orca terminal on the main
  checkout's card (the `orca` surface, preferred by `--surface auto`), so
  the app shows the agent working live and the run survives the dispatching
  session. It falls back to a background log when Orca isn't running.
- One terminal per task; worktrees guarantee runs never trample each other.
  Keep an interactive `claude` in the main checkout for grooming issues,
  exploring, and writing acceptance criteria.
- `agent implement` streams the underlying agent's activity live — every
  tool call (`⚙ Bash: uv run pytest -q`) and thought line — interleaved with
  the stage log (planning → attempts → gates → verdict), so a terminal
  always shows what the agent is actually doing. Set `runtime.stream: false`
  in config for quiet output.
- `agent worktree list` reconciles what's actually in flight if you lose
  track of terminals.
- `agent implement` pushes progress to each task worktree's Orca card —
  comment (`#N: planning` → `implementing` → `PR opened …` / `FAILED gates`)
  and card status (`in-progress` → `in-review`) — best-effort, so runs
  behave identically when Orca is closed.
- Use Orca's diff/PR/CI views to review a run's branch before merge or
  promote. Reviewing there is fine; merging goes through `agent`.
- **Don't use Orca's native spawn-agent-in-worktree feature here.** It
  bypasses the loop entirely: no plan/review fan-out, no gates, no merge
  caps, no blocked-path protection.
- **Don't create or remove worktrees from Orca's UI.** Lifecycle belongs to
  `agent implement` / `agent worktree remove`; a half-removed worktree
  blocks the next run for that task, and concurrent `worktree add` from two
  tools invites the git config-lock contention the platform retries around.

Orca replaces scattered terminal windows; it doesn't replace the platform's
gates — a run only becomes a PR when tests, lint, and self-review pass,
regardless of which terminal it ran in.

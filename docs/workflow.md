# Day-to-day workflow

How work actually flows through the platform: capture → groom → dispatch →
review → merge, with GitHub Projects as the board and Orca as the cockpit
for parallel runs.

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
issue, the agents can't see it.

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

The label is the contract, but the human gate is **dispatch and merge** —
nothing runs without `agent implement`, nothing lands without your merge.
The CI lane's stricter equivalent is `approved-for-agent` (see
`prompts/orchestrator.md`); that one stays human-only.

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
  `board.yml` (the Projects-board repo list). Copy the committed
  `config/board.example.yml` there and fill it in on a new machine.
- The CI lane has no central registry at all: each managed repo carries its
  own stub workflow and passes its settings as workflow inputs, so managed
  repo names only ever appear inside the managed repos themselves.
- History was scrubbed (git-filter-repo) before the repo went public, so old
  revisions of these files are gone from every branch.

## GitHub Projects (the board)

Use one user-level Projects v2 board across all your repos as the human view;
agents never read the board — they key off labels, which are visible in
`gh issue view` and survive in the issue itself.

Setup (once, in the GitHub UI):

1. Create a user Project "Dev board" with a Status field:
   `Backlog → Ready (agent) → In progress → In review → Done`.
2. Enable the built-in workflows *Item closed → Done* and
   *Pull request merged → Done*.
3. Feed issues in from every repo. The built-in **Auto-add** workflow is
   limited to ONE per project on the Free plan (it can watch only one repo),
   so use it for your busiest repo and copy
   `stubs/managed-repo-project-sync.yml` into each additional repo — an
   `actions/add-to-project` workflow that adds `agent-ready` issues to the
   board with no repo limit (needs a classic PAT with `project` scope; see
   the stub's header). One-offs: `gh project item-add <number> --owner @me
   --url <issue-url>`.
4. Optional CLI access needs an extra scope: `gh auth refresh -s project`,
   then `gh project item-list <number> --owner @me`.

Convention: moving a card to **Ready (agent)** means you add the
`agent-ready` label (Projects workflows can't add labels; the label is the
source of truth, the column mirrors it). PRs opened by `agent implement`
carry `Closes #N`, so merge closes the issue and the board sweeps itself.

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

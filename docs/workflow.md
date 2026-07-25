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
funnel as yours. In CI it runs daily via the scout lane (01:00 Asia/Bangkok,
feeding the same morning's triage and groom runs); run it locally whenever
the queue runs dry.

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

Spec and plan run in CI too (`stubs/managed-repo-spec.yml` /
`stubs/managed-repo-plan.yml`) — the same `agent spec` / `agent plan` code
paths executed in Actions, so the output can't drift between lanes. Unlike
groom they are label-gated, not scheduled: add `spec-requested` or
`plan-requested` (or dispatch the workflow with an explicit issue number),
and the pipeline runs the CLI, posts the "## Agent spec" / "## Agent plan"
comment, and removes the request label on success. `needs-human`/`blocked`
issues are always skipped, runs are capped by `max_issues`, and both lanes
share the `agent-triage-<repo>` concurrency group with triage/groom so a
repo is never specced while it's being groomed. None of this moves a gate:
spec and plan are read-only + comment-only, and the human still flips the
label to `agent-ready` and owns dispatch/merge exactly as above.

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

Both `agent dispatch` and `agent implement` refuse to start when an open PR
already references the issue (branch `fix/issue-N`, or a real GitHub closing
reference — `Fixes`/`Closes`/`Resolves` `#N` — as GitHub itself parses it;
a bare `#N` mention elsewhere in the title/body does not count) — this is
what stops the local lane and the CI lane from fixing the same issue twice.
The message names the existing PR; pass `--force` to implement anyway (e.g.
the match was a false positive).

### Resuming a self-review halt

If self-review requests changes, `agent implement` stops and keeps the
worktree instead of committing. That halt is not silent: the findings are
saved to `.agent-runs/issue-<N>-feedback.md` under the project root (not the
worktree, so they can't get swept into a later `git add -A`), and a `## Agent
self-review` comment goes on the issue — a human-visible marker that work is
already sitting in a worktree rather than not yet started. Posting the comment
is best-effort; a repo without a `gh` remote (e.g. a test checkout) just skips
it.

`agent queue` does not read it: the queue lists open `agent-ready` issues and
a halt doesn't remove that label, so a halted issue still appears there.
Re-dispatching one is blocked, but by the worktree already existing — the
error points at `agent worktree remove`, and `agent resume <N>` is what you
usually want instead.

```sh
agent resume 123                       # implementer role, fed the stored self-review findings
agent resume 123 -m "also cover the empty-input case"   # override with different feedback
agent resume 123 --message-file notes.md
```

`agent resume` finds the task's existing worktree (erroring clearly, not with
a traceback, if there isn't one), attaches to a surface the same way `agent
dispatch` does, and runs the same loop → self-review → PR tail as
`agent implement`. Feedback always reaches the agent as a file — inside the
worktree-spawning surface's argv it is only ever a path, never inlined text —
so it can't be mangled by shell quoting the way a hand-rolled
`orca terminal create --command "$(cat …)"` invocation can.

## 4. Review & merge — humans own main

```sh
agent review 45 --post      # agent pre-review as a PR comment
gh pr checkout 45           # your own look
gh pr merge 45 --squash
```

Reviewing everything open before a promote is one command, not a shell loop:
`agent review --all` (or `agent review 169 168 167`) runs the reviews
concurrently (`--jobs`, default 3) and prints one summary line per PR —
`APPROVE` / `REQUEST CHANGES` / `run failed`. If a run fails because the
model ladder itself is exhausted, the rest of the queue aborts rather than
burning it on the same wall.

The agent review is a pre-filter, never the approval. Merge is always yours.

## 5. Background — the CI lane

The scheduled triage pipeline handles the long tail (triage, small fixes,
audit issues) across repos registered in `config/repos.yml`. Check its run
summaries and `needs-human` labels once a day; that's your ops inbox. It
skips issues that already have an open PR, the symmetric half of the local
lane's guard above, so the two lanes never fix the same issue twice.

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
  `agent status --pipelines` shows which reusable CI lanes each registered
  repo has wired up, read live from its workflow files via the GitHub API.
- History was scrubbed (git-filter-repo) before the repo went public, so old
  revisions of these files are gone from every branch.

## Model fallback (when a tier goes unavailable mid-run)

Roles reference tiers (`smart`, `fast`), and `model_tiers` maps each tier to a
concrete model **per runtime**. When a run fails because that model cannot
serve the account — monthly spend limit hit, model unsupported for the auth in
use, model retired — the run steps down `model_fallbacks` instead of dying:

```yaml
model_tiers:
  claude_code:
    smart: fable
    fast: sonnet
model_fallbacks:            # the full ladder, best first
  claude_code:
    smart: [fable, opus, sonnet]
    fast: [sonnet, haiku]
```

Rules worth knowing:

- **Only availability advances a rung.** A rate limit or an overload retries
  the *same* model (the CLIs already back off internally); a failing gate or a
  bad agent run never changes the model at all.
- **Substitutions are loud.** The log says `MODEL FALLBACK: …`, and every
  artifact the run posts — PR review comment, plan/spec comment, PR body —
  names the model that produced it.
- **A substitution holds for the rest of the run**, so a retry after a failed
  gate does not walk back into a model that just refused.
- **Inert unless a rung is unavailable.** `config/defaults.yaml` ships a
  ladder for the `claude_code` runtime out of the box; a project can override
  or clear it via its own `model_fallbacks`, and a run that never hits an
  availability failure makes exactly the calls it would have made anyway.
- **Override `model_tiers`, override `model_fallbacks` too.** The ladder is
  trimmed to the rungs *below* the active model, but only when that model
  actually appears on the ladder. A project that repoints, say,
  `model_tiers.claude_code.smart` at `haiku` while inheriting the default
  `smart: [fable, opus, sonnet]` keeps the ladder whole — so an availability
  failure steps *up* into models it never chose and never budgeted for. Set
  both keys together, or clear `model_fallbacks` for that tier.
- `agent doctor` prints the resolved model and ladder for every role.

### Refreshing the ladder

The tiers self-update only in the sense that `fable`/`sonnet` are floating
aliases — that says nothing about which model is a sensible *fallback*, and it
rots silently when a model is retired. Check the ladder against the Models API
rather than from memory:

```sh
# every model this account can actually use, newest first
curl -s https://api.anthropic.com/v1/models \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  | jq -r '.data[] | "\(.id)\t\(.display_name)"'

# capabilities and limits for one candidate rung
curl -s https://api.anthropic.com/v1/models/claude-opus-5 \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  | jq '{id, max_input_tokens, max_tokens, capabilities}'
```

A rung is only worth adding if it is still listed, its context window is large
enough for the role's prompts (review diffs are big), and it is cheaper or at
least no more expensive than the rung above it. `config/defaults.yaml` is a
human-reviewed file: propose ladder changes in a PR, do not let an agent land
them.

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
- `agent dispatch <N>` creates the task's `fix/issue-N` worktree up front
  and spawns each run in an Orca terminal attached to that worktree's card
  (the `orca` surface, preferred by `--surface auto`), so the app shows the
  agent working live under the issue it belongs to and the run survives the
  dispatching session. `agent implement` takes over the pre-created
  worktree. It falls back to a background log (kept under the main
  checkout's `.agent-runs/`, where it outlives the worktree) when Orca
  isn't running.
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

### Mirroring the CI lanes into Orca

Locally dispatched runs push to their own cards, but the scheduled CI lanes
run on a GitHub Actions runner and leave nothing behind locally. `agent status
--sync-orca` closes that gap: it reads open agent PRs and `agent-ready` issues
across every registered repo, checks out each PR's branch under the repo's
`.worktrees/` so the diff is reviewable in the app, and sets each card's
comment and status from the PR's check results.

    agent status --sync-orca

- **Read-only towards GitHub.** It never comments, labels, merges, or pushes;
  the only things it writes are local checkouts and Orca card metadata.
- **Idempotent.** Cards are keyed by branch, so re-running adopts what exists
  rather than duplicating it, and a card that already says the right thing is
  left untouched. Run it as often as you like — e.g. from an
  `orca automations` job while the app is open.
- **A no-op without Orca.** Closed app or no `orca` CLI prints one line and
  exits 0.
- Statuses map from checks: green → `in-review`, failing/running/draft →
  `in-progress`, queued issue → `todo`.
- It never *removes* a card and never touches a card for a queued issue: a
  live local `agent implement` run may own that card, and the viewer must not
  fight the thing it is watching. Clean up merged lanes with
  `agent worktree remove <task-id>` as usual.
- First run for a new lane pauses ~15-30s: Orca notices external worktrees on
  a periodic rescan, so the sync waits once per batch. If it still reports
  `not indexed by Orca yet`, just re-run it.

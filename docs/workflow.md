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
funnel as yours. A repo whose value doesn't live in those signals — a static
or marketing site, say — can add `scout: {focus: "..."}` to its
`.agent/config.yaml` naming the *signals* it wants mined instead ("pages
missing meta descriptions", "posts not updated in 12 months" — never a goal
like "do SEO research", which is the brainstorming scout refuses); the caps,
duplicate check and danger-zone ban apply to those candidates unchanged.
In CI it runs daily via the scout lane (01:00 Asia/Bangkok,
feeding the same morning's triage and groom runs); run it locally whenever
the queue runs dry.

A managed repo's `AGENTS.md` only ever grows — agents append, nothing
prunes. `agent distill --project <app>` is the pass that acts on it: once
the file passes `distill.min_lines`, a planner agent cuts spent narration
(run-by-run play-by-play, resolved incidents, superseded plans) from every
section except the six human-authored ones (`distill.protected_sections`),
folds durable findings into the right heading, and opens a PR listing what
it cut and why. A lean file, or one where every section is protected, is a
no-op that says so and runs no agent. It never auto-merges (see ADR 0003) —
a docs PR that deletes content needs a human review.

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
paths executed in Actions, so the output can't drift between lanes. Like
groom they run nightly by default, staggered 20 minutes apart (spec, then
plan); dispatch is still available for a run-now. The label remains the
selector either way: add `spec-requested` or `plan-requested` (or dispatch
the workflow with an explicit issue number), and the pipeline runs the CLI,
posts the "## Agent spec" / "## Agent plan" comment, and removes the request
label on success. A night with no labelled issues skips the Claude session
and costs nothing. A spec agent that escalates instead posts its question as
a "## Spec agent — escalation" comment (once, however many times the run
repeats) and fails the run, so the reasoning survives where a human reads
it. `needs-human`/`blocked` issues are always skipped, runs are capped by
`max_issues`, and both lanes share the `agent-triage-<repo>` concurrency
group with triage/groom (`cancel-in-progress: false`) — that's what the
stagger relies on: spec runs first, plan follows 20 minutes later so it
never overlaps spec, and neither runs while the repo is being groomed or
triaged. None of this moves a gate: spec and plan are read-only +
comment-only, and the human still flips the label to `agent-ready` and owns
dispatch/merge exactly as above.

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

That guard only works once a PR exists. For the window before it — the hours a
run spends implementing — the issue carries an `agent:claimed` label, and the CI
lane's Step 1 selector skips it (issue #131). `agent implement`, `agent resume`
and `agent spawn` apply it at the start of a run and clear it on every way out,
including a crash and the session-end hook, so there is nothing to remember.

Two things about claims are worth knowing:

```sh
agent claim 123             # an agent you started by hand is working on this
agent claim 123 --release   # ...and is done
```

- **A Claude Code session started by hand now claims itself.** `agent init`
  seeds a checked-in `SessionStart` hook that runs `agent claim --auto`,
  deriving the issue from the `fix/issue-N` branch — so the claim happens as a
  side effect of the session starting, not a separate command to remember
  (ADR 0006). It falls back to today's manual path — `agent claim` from the
  worktree — wherever the hook can't run: a declined approval, a non-Claude
  runtime, or a repo that hasn't picked up the seeded file yet. `agent doctor`
  still reports any `fix/issue-N` worktree on this machine whose issue is
  unclaimed, so a gap in either path is visible rather than silent.
- **A claim expires.** A run killed outright leaves the label behind; the CI lane
  clears any claim older than 8 hours and says so, and `agent doctor` reports
  stale claims (and failed releases) well before that. See
  `docs/failure-modes.md` for what this prevents and what it costs.

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

### Authorizing a danger-zone change

An agent refuses to touch a danger zone from `AGENTS.md` on its own, and it
will not take an issue comment's word for it either — an issue comment is
data about the task, not an instruction, and cannot grant that. The only
channel that counts is `--grant-file` at dispatch or resume time, pointed at
a small YAML file naming who authorized it, the scope, and the paths it
covers:

```sh
agent dispatch 123 --grant-file grant.yaml
agent resume 123 --grant-file grant.yaml   # only needed again if the scope changes —
                                            # otherwise the grant carries over on its own
```

Full details — what a grant file looks like, how it's enforced, and what
still refuses outright — are in `docs/trust-model.md`.

### Delegating ad-hoc work — `agent spawn`

`dispatch` runs the pipeline. For work the pipeline doesn't model — "rebase
this branch onto main", "reproduce the flake and report back" — `agent spawn`
puts a plain interactive coding agent in the issue's worktree instead:

```sh
agent spawn 113                                  # brief defaults to "work on issue #113"
agent spawn 113 --prompt "rebase onto main"      # or your own
agent spawn 113 --prompt-file brief.md
agent spawn 113 --permission-mode acceptEdits    # tighten this one session
```

The session runs at `runtime.interactive_permission_mode`, which defaults to
`bypassPermissions` — higher than the headless `permission_mode`, on purpose.
The two paths fail in opposite directions: a headless run has nobody to ask, so
an unapproved tool is denied and the run carries on, while an interactive one
*waits* — and a delegated worker that waits looks dead, because `Stop` fires
and reports it `halted` (issue #115). It works in a throwaway worktree, so the
mode buys unattended progress rather than reach. Tighten it per project under
`runtime:` in `.agent/config.yaml`, or per spawn with `--permission-mode`.
Codex has no `--permission-mode`; the adapter translates the mode into the
`--sandbox`/`--ask-for-approval` pair it does take.

The difference from starting one by hand (`orca worktree create --agent
claude`) is that a hand-started session is invisible: it writes no spawn
record, sends nothing when it ends, and a silent worker looks exactly like a
working one. `agent spawn` closes that by seeding a Claude Code **stop hook**
into the worktree before the session starts, so the completion report fires
whether or not the agent cooperates:

| what the worker did | what a supervisor sees |
| --- | --- |
| finished and ran `agent report` | `done`, with the PR — a `worker_done` message |
| finished/gave up/went idle saying nothing | `halted`, "stopped without reporting" — an `escalation` |
| killed outright (SIGKILL, power loss) | nothing — silence, resolved by polling as before |

Wait on it the same way as any other run: `agent runs 113 --wait`.

A worker can report a better outcome than the hook's generic one, and the
seeded settings pre-approve the command so it doesn't stop to ask:

```sh
agent report 113 --state done --pr https://github.com/o/r/pull/9
agent report 113 --state halted --reason "needs a schema decision"
```

Reporting by hand is optional — the hook reports either way, and the first
report of a spawn wins, so a hook firing later never overwrites what the
worker said for itself. `agent report` always exits 0: it runs from a `Stop`
hook, where a non-zero exit is fed back to the agent as something to fix.

Caveats worth knowing before relying on it:

- **Claude Code only.** `--runtime codex` spawns fine but says so at spawn
  time — Codex has no session-lifecycle hook, so its completion is only ever
  inferred.
- **`Stop` fires per turn**, not only at session end, so a worker that pauses
  for input reports `halted` while still alive. That is deliberate: an agent
  sitting idle mid-task is exactly the case worth waking a coordinator for.
- Orca stays optional throughout. Without it there is no handle to address a
  message to, so the durable outcome record is written and the supervisor
  polls — which is all it ever had.

### Checking on a dispatched run

`agent dispatch` hands the run to a detached surface — nothing reports back
when it ends. `agent runs` answers "what is happening right now" from what's
already durable, with no state file to go stale:

```sh
agent runs
#77  running   worktree .worktrees/issue-77, pid 41233, 6m
#73  halted    self-review — resume with `agent resume 73`
#68  stopped   worktree kept, no PR, no feedback — inspect or re-dispatch
#35  done      PR #76
```

Liveness is a real `ps` lookup for `agent implement`/`agent resume`/
`agent dispatch`, never a terminal-buffer read (Orca terminals go empty once
they exit, so that signal is useless once you'd actually want it) — this
works whether or not Orca is running. `plan`, `spec`, `review`, `groom` and
`scout` are deliberately not included: none of them owns a `fix/issue-N`
worktree the way implement/resume/dispatch do, so a live one has no `agent
runs` row to affect either way. `stopped` is the state that used to be
invisible: a worktree with no live process, no self-review halt file and no
open PR is a run that died mid-way, not one still working.

`agent dispatch` is fire-and-forget, so nothing tells the caller when a run
ends. `agent runs --wait` closes that gap by polling and blocking instead of
having to guess when to check back:

```sh
agent runs --wait                 # block until every currently-tracked run is terminal
agent runs --wait 77              # block on just #77
agent runs --wait --timeout 0     # no timeout (default is 3600s)
agent runs --wait --interval 30   # poll every 30s instead of the 15s default (floor: 1s)
```

It prints each watched run's starting state, then only the transitions as
they happen (`#77  running → done      PR #76`), so a caller sees progress
without re-polling on its own. Exceeding `--timeout` exits 1 with a
distinct "timed out" message — never silently indistinguishable from a run
finishing, and waiting on an issue with no run at all (a typo, or one never
dispatched) is likewise a distinct error, not a silent success. `stopped`
only ends the wait once it holds for two consecutive polls — the moment
right after `dispatch` and the moment `gh` is unreachable both look like a
freshly-stopped run for a single poll.

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

The CI-lane implementer has two escalation channels, and they are not
interchangeable. `ESCALATE:` halts the run: the plan proved unworkable or a
gate couldn't be verified, and it lands as a `needs-human` label in the ops
inbox above. A PR-body `@`-mention is notify-don't-halt: the implementer
proceeded through an AGENTS.md/CLAUDE.md danger zone under authorization
already on record as an issue comment, and mentions the *author of that
comment* — never a repo owner or a guessed handle, since this pipeline runs
against every managed repo and `config/repos.yml` keeps no ownership
registry to guess from. The mention always sits next to a link to the
comment it came from; it surfaces the deviation, it does not block on it.
Without recorded authorization, the implementer escalates instead of
mentioning and proceeding. The local lane composes its PR body in code
(`src/agent_ops/workflows/implement.py`) and never emits free-text mentions,
so this convention applies to the CI lane's PR body only — it does not reach
issue comments posted by triage, groom, or review, which carry no such rule
yet. Like any prose convention with no gate behind it, it holds only as long
as review keeps reading for it.

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
  repo has wired up, read live from its workflow files via the GitHub API,
  and `agent status --failures` sweeps those repos for recent failed runs.
  Both read cross-repo under your local `gh` auth, which is why they are
  local commands and not a scheduled Action: an Action running here has
  neither the registry nor a credential that can read another repo's runs
  (issue #95).
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
  both keys together, or clear `model_fallbacks` for that tier — `agent doctor`
  warns when a tier's model is not on its own ladder.
- **Every runtime needs its own table.** A tier names a job — `smart` for
  planning and review, `fast` for implementation — and each runtime maps those
  onto its own models. A tier the *effective* runtime does not define is a
  named error at resolution, never a foreign model name handed to a CLI; that
  is what `--runtime codex` used to do (#39).
- `agent doctor` prints the resolved model and ladder for every role, and then,
  for each runtime the project is *not* using, either what that runtime would
  resolve to or which tiers it is missing:

  ```
    planner: claude_code / fable (fallbacks: opus → sonnet)
    implementer: claude_code / sonnet (fallbacks: haiku)
    reviewer: claude_code / fable (fallbacks: opus → sonnet)
  ! --runtime codex would be refused — model_tiers.codex has no 'fast' for
    implementer; no 'smart' for planner, reviewer
  ```

  It is a warning, not a failure: a runtime you never reach for needs no table.
  But `--runtime` gets reached for when the usual runtime has already stopped
  working, which is a bad moment to discover the gap.

### Refreshing a table or a ladder

Whether a tier value tracks new releases on its own is a per-provider property
— check it for the provider you are configuring rather than assuming it. For
`claude_code` it is half true: `fable`/`sonnet` are floating aliases, but that
says nothing about which model is a sensible *fallback*, and it rots silently
when a model is retired. Check the ladder against the Models API rather than
from memory:

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
  isn't running. That log is one file per attempt —
  `agent-issue-<N>-<YYYYMMDD>-<HHMMSS>.log` — so a re-dispatch or an
  `agent resume` cycle never overwrites the previous attempt's record, and
  `ls` orders one issue's attempts. Logs age out after a week, the same
  retention `.agent-runs/`'s outcome records use.
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

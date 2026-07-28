# Lane reference: local vs. CI

Nine lanes — groom, scout, spec, plan, evolve, triage, implement, review,
merge — each run in two surfaces: the local `agent` CLI and the CI pipelines
in `.github/workflows/`. "Local" and "CI" are two places the same code can
run, not two designs; for six lanes below they are, in fact, the exact same
code path (review joined that list in #171, evolve shipped that way from the
start in #153). The other three still run at least partly as prompt prose in
`prompts/orchestrator.md` on the CI side — that split is history
(`orchestrator.md` predates the CLI having those commands), not intent, and
it is the exception being retired by the convergence work tracked in #171.

This page is a reference, not a tutorial: every cell cites the file and line
it was checked against. If a citation has drifted, that is this page falling
out of date — fix the page, don't route around it.

## The table

| Lane | Local | CI | Same implementation? |
| --- | --- | --- | --- |
| groom | `agent groom` — `src/agent_ops/cli.py:772` (`groom`) → `workflows/groom.py` | `uv run agent groom` — `.github/workflows/groom-pipeline.yml:99` | yes |
| scout | `agent scout` — `src/agent_ops/cli.py:788` (`scout`) → `workflows/scout.py` | `uv run agent scout` — `.github/workflows/scout-pipeline.yml:90` | yes |
| spec | `agent spec` — `src/agent_ops/cli.py:652` (`spec`) → `workflows/spec.py` | `uv run agent spec` — `.github/workflows/spec-pipeline.yml:155` | yes |
| plan | `agent plan --post` — `src/agent_ops/cli.py:607` (`plan`) | `uv run agent plan --post` — `.github/workflows/plan-pipeline.yml:154` | yes |
| evolve | `agent evolve <lane>` — `src/agent_ops/cli.py:818` (`evolve`) → `workflows/evolve.py:583` (`run_evolve`) | `uv run agent evolve "$LANE"` — `.github/workflows/evolve-pipeline.yml:160`, one call per lane via the weekly sweep matrix in `evolve.yml` (#153) | yes |
| triage | `src/agent_ops/cli.py:751` (`triage`) → `workflows/triage.py:65` (`run_triage`) | `.github/workflows/triage-pipeline.yml:196` (`claude-code-action` step) running `prompts/orchestrator.md` Step 1 (lines 46-80) | **no** |
| implement | `src/agent_ops/cli.py:227` (`implement`) → `workflows/implement.py:228` (`run_implement`) | `prompts/orchestrator.md` Step 2A item 2, line 86, role file `prompts/agents/implementer.md` | **no** |
| review | `src/agent_ops/cli.py:672` (`review`) → `workflows/review.py:84` (`run_review`), fan-out at `workflows/review.py:161` (`run_reviews`) | `uv run --project agent-ops agent review <PR_NUMBER> --project target --post --check` — `prompts/orchestrator.md` Step 2A item 4, line 89 | **yes** (#171) |
| merge | `agent merge --check` — `src/agent_ops/cli.py:889` (`merge`) → `workflows/merge.py:171` (`run_merge`) / `workflows/merge.py:43` (`evaluate_merge`) | `prompts/orchestrator.md` Step 3, line 148, shells out to `workflows/merge.py:142` (`run_merge_check`) for the same `evaluate_merge` | **partially** (#150) |

## The three diverged lanes

**triage.** Local `run_triage` (`src/agent_ops/workflows/triage.py:65`) renders
`prompts/tasks/triage.md`, parses a `TRIAGE RESULTS:` block out of the model's
reply, and classifies unbucketed issues into `agent-ready` / `needs-human` /
`backlog`, with an optional `--dispatch` to hand qualifying issues straight to
implement. CI triage is `prompts/orchestrator.md` Step 1 (lines 46-80), run by
`claude-code-action` inside `.github/workflows/triage-pipeline.yml` (the step
starts at line 196; the prompt is assembled at lines 219-227, with
`AUTO_MERGE` passed at line 222). It does
everything the local lane does, plus things the local lane does not: it routes
P0 bugs to a hotfix lane (Step 2B), clears stale `agent:claimed` labels older
than 8 hours, closes duplicates, answers verifiable questions, caps selection
at 3 issues per run, and stamps `triage:done` — a label the local lane never
writes or reads (see the comment at `src/agent_ops/workflows/triage.py:17-19`,
which exists precisely so the local lane keeps triaging issues the CI lane
marked done but left bucketless). `agent status --pipeline` counts how many
open issues carry each of these stage labels, fleet-wide, and how long the
oldest one in each stage has sat there (issue #227) — the thing this table
of labels doesn't show on its own.

**implement.** Local `run_implement` (`src/agent_ops/workflows/implement.py:228`)
works in an isolated git worktree, runs a plan stage, then a gate loop that
retries up to `loop.max_attempts` (default 3, `src/agent_ops/config.py:46`)
times with a fresh context on failure, plus a coded self-review pass and
claim/release bookkeeping (#131) so two runs can't collide on the same issue.
CI implement is a single subagent inside the Actions workspace
(`prompts/orchestrator.md` Step 2A item 2, line 86, role file
`prompts/agents/implementer.md`): no worktree, no coded gate loop — the only
retry is the orchestrator's one prose-driven revision round after a Tester
FAIL (Step 2A, "Failure handling", lines 105-113).

**merge — partially converged.** This row differs from the issue that
prompted this page: #150 ("CI lane and `agent merge` now disagree on what a
cap counts") is CLOSED, and the fix (ADR
[`docs/adr/0005-one-merge-cap-evaluator.md`](../adr/0005-one-merge-cap-evaluator.md))
converged the *cap evaluation* itself. Diff-size and blocked-path caps are now
judged by one function, `evaluate_merge` (`src/agent_ops/workflows/merge.py:43`),
on both sides: local `agent merge` calls it via `run_merge`
(`src/agent_ops/workflows/merge.py:171`), and CI Step 3
(`prompts/orchestrator.md:148`) shells out to
`uv run --project agent-ops agent merge <PR> --project target --check`, which
is `run_merge_check` (`src/agent_ops/workflows/merge.py:142`) wrapping the same
`evaluate_merge`. What is still diverged is the merge *decision and action*:
on CI that remains orchestrator prose (`prompts/orchestrator.md:160-171`) —
the Tester PASS + `agent review --check` gate, the open-ended "no infra files
the check doesn't cover" rule, the `AUTO_MERGE` report-only toggle, and the
squash itself are all judged and performed by the orchestrator model, not by
code. Local `run_merge` performs the CI-green check and the squash in code.
Hence "partially" rather than "yes."

## `review` — converged (#171)

Before this pass, CI review was a Reviewer subagent in Step 2A item 4
(formerly `prompts/orchestrator.md:88`; that line now holds the review gate
described below, role file `prompts/agents/reviewer.md`; see the note below on
what this change did to Step 2B's reference to the same role file)
emitting free-text APPROVE / REQUEST CHANGES that the orchestrator model then
interpreted — the same fail-open shape #159 had already fixed for the local
lane's `prompts/tasks/review.md` (a rejection phrased loosely, or containing
the word "approve" mid-sentence, could read as approval). It also received
different inputs than `run_review` (issue text and the Tester's PASS/FAIL
verdict) and had no diff-line budgeting.

Step 2A item 4 now shells out to `agent review --check`
(`src/agent_ops/cli.py:672`), which runs the exact same `run_review` /
`verdict_of` code path as the local lane, honours its exit code and printed
`VERDICT:` line, and feeds that into both the Step 2A revision-round rule and
the Step 3 merge gate (`prompts/orchestrator.md:89-100,108-113,164-165`).
`prompts/agents/reviewer.md` was also anchored to require the same `VERDICT:`
line, since it is still the role file Step 2B's hotfix lane spawns as a
subagent — that lane is unconverged and out of scope for #171 (see below for
what converting Step 2A's item 4 did to Step 2B's reference to that file).

Converting Step 2A's item 4 from a subagent to a shelled-out check removed the
orchestrator's only other reference to `prompts/agents/reviewer.md`, so Step
2B's "run the same four agents" (which named no agents) stopped resolving to
anything — the hotfix lane's review step had gone undefined. Step 2B item 2
(`prompts/orchestrator.md:124-125`) now names all four agents explicitly,
including `REVIEWER (prompts/agents/reviewer.md)`, restoring what the
reference used to resolve to. This is a repair of the reference broken by the
review-lane convergence, not a change to Step 2B's behavior: its overrides,
revision rounds, and merge rule are untouched and it still runs the Reviewer
as a subagent rather than the CLI gate.

Two accepted, documented differences remain rather than silent drops: CI no
longer passes issue text or the Tester's verdict into the review (a
deliberate scope decision, not a widened `run_review` signature), and
`--post` leaves a PR comment rather than a formal GitHub "review" resource —
if a target repo's branch protection requires the latter, this alone will
not satisfy it.

## `auto_merge` and `config/repos.yml`

The original issue's claim — that `config/repos.yml`'s `auto_merge` block is
"currently read by nothing" — was true when the issue was filed and has since
been resolved by #150. `config/repos.yml` no longer has an `auto_merge` block
at all; its comment now states plainly that merge caps and blocked paths live
in each managed repo's `.agent/config.yaml` (`merge:` section) and are
enforced by `evaluate_merge` / `agent merge --check`, the same code path
CI Step 3 calls. `src/agent_ops/registry.py:12` reads only
`config/local/repos.yml` (a git-ignored, bare list of managed repos), never
`config/repos.yml`.

`auto_merge` as a name survives in two unrelated, still-live places: the
`auto_merge` `workflow_call` input on the triage pipeline (report-only merge
toggle, `.github/workflows/triage-pipeline.yml`, consumed at line 222 as
`AUTO_MERGE` in the orchestrator prompt), and `loop.auto_merge`
(`src/agent_ops/config.py:64`, default `False`) for the local implement lane,
checked at `src/agent_ops/workflows/implement.py:455` to decide whether a
freshly opened PR is immediately run through `run_merge`.

## Trust model

Every local-lane prompt reads GitHub content — issue bodies, comments, PR
descriptions, review threads, CI logs, diffs — that anyone who can open an
issue or comment on a managed repo can write, and some of it (dependency-bot
PR bodies, CI output) no human writes at all. `render_task`
(`src/agent_ops/prompts.py`) prepends the same untrusted-data guard
(`prompts/untrusted-data.md`) to every `prompts/tasks/*.md` template before
it reaches a model: the prompt template and the target repo's `AGENTS.md` /
`CLAUDE.md` are authoritative, everything else is data to reason about, and
an agent that notices injected instructions says so rather than silently
following or ignoring them. `scout`'s repo-focus block
(`focus_block`, `src/agent_ops/workflows/scout.py`, #140) is the trusted end
of the same spectrum: repo-authored text a maintainer configures, held to the
same authority as `AGENTS.md`.

`prompts/orchestrator.md` (the CI lane) does not yet carry this guard — it
renders straight off disk (`.github/workflows/triage-pipeline.yml`), not
through `render_task`, and is a human-reviewed danger zone held by the #171
convergence work. Tracked as a follow-up rather than folded into this file.

## What happens next

- [#171](https://github.com/jirathip-k/agent-ops/issues/171) — converge
  triage, review, and implement onto one implementation instead of two. The
  review row above is the first to flip; triage and implement remain "no."
- [#150](https://github.com/jirathip-k/agent-ops/issues/150) — closed; the
  merge-cap convergence documented in the merge row above.
- [#139](https://github.com/jirathip-k/agent-ops/issues/139) — open, not yet
  landed: an encyclopedia mapping platform vocabulary to source. Linked here
  as the issue, not as a page, until it lands.

## Footnote: a ninth lane

`promote` also runs on both surfaces and is already fully converged: local
`agent promote` (`src/agent_ops/cli.py:916`, `promote`) and
`.github/workflows/promote-pipeline.yml` both open the staging → stable
promotion PR through the same code path. It is not in the table above because
the issue that requested this page scoped it to the eight lanes with a
CI-prompt divergence risk; `promote` never had one.

## Footnote: a tenth lane, dispatch-only by decision

`distill` also runs on both surfaces through one code path: local `agent
distill` (`src/agent_ops/cli.py:965`, `distill`) → `run_distill`
(`src/agent_ops/workflows/distill.py:168`), and CI's
`uv run agent distill` (`.github/workflows/distill-pipeline.yml:132`) via
`stubs/managed-repo-distill.yml`. It is not in the table above for the same
reason `promote` isn't — no CI-prompt divergence risk to track.

Unlike every cron-scheduled lane above, its stub ships `workflow_dispatch`
only, with no `schedule:`. That is a deliberate decision
([#198](https://github.com/jirathip-k/agent-ops/issues/198)), not an
oversight: distill prunes `AGENTS.md` against a fixed allowlist
(`DistillConfig.protected_sections`) that a human can silently fall out of
sync with by adding a heading, and it has no evidence gate the way `evolve`
waits on `--min-runs` — its only trigger, `min_lines`, says nothing about
whether the file *should* be shortened. A schedule can be added later once a
few dispatched runs have been watched; a pruned section a human wrote cannot
be restored by removing one. See #198 before adding a `schedule:` to either
`distill-pipeline.yml` or its stub.

## Footnote: CI-lane commit identity ([#203](https://github.com/jirathip-k/agent-ops/issues/203))

`distill` and `evolve` are the first CLI-lane pipelines to commit to a
branch — GitHub-hosted runners configure neither `user.name` nor
`user.email`, so a plain `git commit` there fails only after the run has
already paid for checkout, setup, and a full agent session. The fix lives in
code, not YAML: `worktree.commit` (`src/agent_ops/worktree.py`) falls back to
a `github-actions[bot]` identity only when git has none configured anywhere
(global, system, repo, or env) — a local `agent distill` / `agent evolve`
still commits as the developer. `implement.py`'s `_finish_run` uses the same
helper. A `git commit` shelled out anywhere else fails
`tests/test_commit_identity_drift.py`. The `git config --global` steps
already in `distill-pipeline.yml` and `evolve-pipeline.yml` are redundant
with this fallback but kept — removing them is a workflow-YAML edit outside
this fix's scope.

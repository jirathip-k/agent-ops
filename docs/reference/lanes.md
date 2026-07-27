# Lane reference: local vs. CI

Eight lanes — groom, scout, spec, plan, triage, implement, review, merge —
each run in two surfaces: the local `agent` CLI and the CI pipelines in
`.github/workflows/`. "Local" and "CI" are two places the same code can run,
not two designs; for four lanes below they are, in fact, the exact same code
path. The other four still run as prompt prose in `prompts/orchestrator.md`
on the CI side — that split is history (`orchestrator.md` predates the CLI
having those commands), not intent, and it is the exception being retired by
the convergence work tracked in #171.

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
| triage | `src/agent_ops/cli.py:751` (`triage`) → `workflows/triage.py:65` (`run_triage`) | `.github/workflows/triage-pipeline.yml:196` (`claude-code-action` step) running `prompts/orchestrator.md` Step 1 (lines 46-80) | **no** |
| implement | `src/agent_ops/cli.py:227` (`implement`) → `workflows/implement.py:228` (`run_implement`) | `prompts/orchestrator.md` Step 2A item 2, line 85, role file `prompts/agents/implementer.md` | **no** |
| review | `src/agent_ops/cli.py:672` (`review`) → `workflows/review.py:84` (`run_review`), fan-out at `workflows/review.py:161` (`run_reviews`) | Reviewer subagent, `prompts/orchestrator.md` Step 2A item 4, line 88, role file `prompts/agents/reviewer.md` | **no** |
| merge | `agent merge --check` — `src/agent_ops/cli.py:889` (`merge`) → `workflows/merge.py:171` (`run_merge`) / `workflows/merge.py:43` (`evaluate_merge`) | `prompts/orchestrator.md` Step 3, line 131, shells out to `workflows/merge.py:142` (`run_merge_check`) for the same `evaluate_merge` | **partially** (#150) |

## The four diverged lanes

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
marked done but left bucketless).

**implement.** Local `run_implement` (`src/agent_ops/workflows/implement.py:228`)
works in an isolated git worktree, runs a plan stage, then a gate loop that
retries up to `loop.max_attempts` (default 3, `src/agent_ops/config.py:46`)
times with a fresh context on failure, plus a coded self-review pass and
claim/release bookkeeping (#131) so two runs can't collide on the same issue.
CI implement is a single subagent inside the Actions workspace
(`prompts/orchestrator.md` Step 2A item 2, line 85, role file
`prompts/agents/implementer.md`): no worktree, no coded gate loop — the only
retry is the orchestrator's one prose-driven revision round after a Tester
FAIL (Step 2A, "Failure handling", lines 93-97).

**review.** Local `run_review` (`src/agent_ops/workflows/review.py:84`) is a
read-only reviewer over a budgeted PR diff, capped by `review.max_diff_lines`
(default 5000, `src/agent_ops/config.py:176`), with an optional `--post` and a
concurrent fan-out for multiple PRs via `run_reviews`
(`src/agent_ops/workflows/review.py:161`). CI review is the Reviewer subagent
in Step 2A item 4 (`prompts/orchestrator.md:88`, role file
`prompts/agents/reviewer.md`) emitting APPROVE / REQUEST CHANGES that feeds
the orchestrator's Step 3 merge gate directly — a different prompt, different
inputs, and no diff-line budgeting.

**merge — partially converged.** This row differs from the issue that
prompted this page: #150 ("CI lane and `agent merge` now disagree on what a
cap counts") is CLOSED, and the fix (ADR
[`docs/adr/0005-one-merge-cap-evaluator.md`](../adr/0005-one-merge-cap-evaluator.md))
converged the *cap evaluation* itself. Diff-size and blocked-path caps are now
judged by one function, `evaluate_merge` (`src/agent_ops/workflows/merge.py:43`),
on both sides: local `agent merge` calls it via `run_merge`
(`src/agent_ops/workflows/merge.py:171`), and CI Step 3
(`prompts/orchestrator.md:131`) shells out to
`uv run --project agent-ops agent merge <PR> --project target --check`, which
is `run_merge_check` (`src/agent_ops/workflows/merge.py:142`) wrapping the same
`evaluate_merge`. What is still diverged is the merge *decision and action*:
on CI that remains orchestrator prose (`prompts/orchestrator.md:143-154`) —
the Tester PASS + Reviewer APPROVE gate, the open-ended "no infra files the
check doesn't cover" rule, the `AUTO_MERGE` report-only toggle, and the squash
itself are all judged and performed by the orchestrator model, not by code.
Local `run_merge` performs the CI-green check and the squash in code. Hence
"partially" rather than "yes."

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

## What happens next

- [#171](https://github.com/jirathip-k/agent-ops/issues/171) — converge
  triage, review, and implement onto one implementation instead of two. This
  is the work that will flip the three remaining "no" rows above.
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

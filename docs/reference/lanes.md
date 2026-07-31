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

This page is a reference, not a tutorial: every cell cites where it was
checked. Citations into Python (`src/agent_ops/**.py`) name the file and the
symbol — function, class, or constant — instead of a line number, so an
insertion above the symbol can't stale them; `tests/test_lanes_citations.py`
resolves each one by searching the file. Citations into YAML
(`.github/workflows/*.yml`) and `prompts/orchestrator.md` still cite a line
number — there is no symbol there to anchor to. If a citation has drifted,
that is this page falling out of date — fix the page, don't route around it.

## The table

| Lane | Local | CI | Same implementation? |
| --- | --- | --- | --- |
| groom | `agent groom` — `src/agent_ops/cli.py` (`groom`) → `workflows/groom.py` | `uv run agent groom` — `.github/workflows/groom-pipeline.yml:99` | yes |
| scout | `agent scout` — `src/agent_ops/cli.py` (`scout`) → `workflows/scout.py` | `uv run agent scout` — `.github/workflows/scout-pipeline.yml:90` | yes |
| spec | `agent spec` — `src/agent_ops/cli.py` (`spec`) → `workflows/spec.py` | `uv run agent spec` — `.github/workflows/spec-pipeline.yml:155` | yes |
| plan | `agent plan --post` — `src/agent_ops/cli.py` (`plan`) | `uv run agent plan --post` — `.github/workflows/plan-pipeline.yml:154` | yes |
| evolve | `agent evolve <lane>` — `src/agent_ops/cli.py` (`evolve`) → `src/agent_ops/workflows/evolve.py` (`run_evolve`) | `uv run agent evolve "$LANE"` — `.github/workflows/evolve-pipeline.yml:160`, one call per lane via the weekly sweep matrix in `evolve.yml` (#153) | yes |
| triage | `src/agent_ops/cli.py` (`triage`) → `src/agent_ops/workflows/triage.py` (`run_triage`) | `.github/workflows/triage-pipeline.yml:196` (`claude-code-action` step) running `prompts/orchestrator.md` Step 1 (lines 46-74), which defers to `prompts/tasks/triage.md` for classification | **partially** (#257) |
| implement | `src/agent_ops/cli.py` (`implement`) → `src/agent_ops/workflows/implement.py` (`run_implement`) | Automatic queue: `prompts/orchestrator.md` Step 2A item 2, line 82, role file `prompts/agents/implementer.md`. Manual exact-issue lane: `uv run agent implement "$ISSUE"` — `.github/workflows/implement-pipeline.yml:138` | **manual yes; automatic no** (#296) |
| review | `src/agent_ops/cli.py` (`review`) → `src/agent_ops/workflows/review.py` (`run_review`), fan-out at `src/agent_ops/workflows/review.py` (`run_reviews`) | `uv run --project agent-ops agent review <PR_NUMBER> --project target --post --check` — `prompts/orchestrator.md` Step 2A item 4, line 85 | **yes** (#171) |
| merge | `agent merge --check` — `src/agent_ops/cli.py` (`merge`) → `src/agent_ops/workflows/merge.py` (`run_merge`) / `src/agent_ops/workflows/merge.py` (`evaluate_merge`) | `prompts/orchestrator.md` Step 3, line 144, shells out to `src/agent_ops/workflows/merge.py` (`run_merge_check`) for the same `evaluate_merge` | **partially** (#150) |

## The three diverged lanes

**triage — partially converged (#257).** Both surfaces now render the exact
same classification definition, `prompts/tasks/triage.md`: local `run_triage`
(`src/agent_ops/workflows/triage.py` (`run_triage`)) renders it via
`src/agent_ops/prompts.py` (`render_task`), parses a `TRIAGE RESULTS:` block
out of the model's reply, and applies the verdict in code; CI's
`prompts/orchestrator.md` Step 1 (lines 46-74, run by `claude-code-action`
inside `.github/workflows/triage-pipeline.yml` — the step starts at line 196;
the prompt is assembled at lines 219-227, with `AUTO_MERGE` passed at line
222) lists issues and then tells the orchestrator model to apply the same task
prompt in full, rather than restating its buckets, bug-priority rule,
stale-claim clearing, duplicate/question handling, or `triage:done` stamp.
Buckets, priorities, and the label itself no longer drift between the two.

What still differs is what wraps the shared definition, not the definition
itself. CI's Step 1 keeps its own orchestration prose — the skip-label
listing, the `agent-ready`/`approved-for-agent` override, the P0-hotfix vs.
normal-lane routing map, and the cap of 3 issues per run — none of which is
classification. More importantly, CI grants the orchestrator model
issue-write tools, so the shared prompt's Housekeeping section — applying the
bucket label and posting the `**Triage: <bucket>** — <reason>` comment,
clearing a stale `agent:claimed` past 8 hours, closing duplicates/invalids,
answering and closing verifiable questions — actually executes there,
performed by the model itself. The local surface's prompt invocation grants
only `gh issue create/list` and `gh search issues` (for filing audit
findings) — no `gh issue edit`/`close`/`api`/`comment` — so the model there
only classifies; local `src/agent_ops/workflows/triage.py` (`run_triage`)
applies the bucket label and posts the same comment in code afterward, and the
same gating rule means local never clears claims, closes duplicates, or
answers questions itself.

**What counts as settled differs between the two surfaces, and this is a
real, accepted divergence, not an oversight.** Local (`run_triage`) treats
ANY bucket label as settled, regardless of `triage:done` — see the comment
above `src/agent_ops/workflows/triage.py` (`BUCKET_LABELS`), #257 follow-up:
a bucket label is authoritative no matter who applied it or when, because the
classifying model never sees an issue's existing labels — only number, title
and body reach the prompt — so it cannot be trusted to honor one it can't see.
An earlier version of this guard required `triage:done` alongside the bucket,
which let a human-applied or pre-#257 bucket fall through to be
reclassified — the local lane's own `gh issue edit` could then have stacked a
contradicting verdict on top of a `needs-human` hold. `triage:done` with no
bucket is the one case NOT settled by this rule, and it only ever reaches
`run_triage` as a still-*open* issue: a legacy issue stamped before the
bucket-alongside-stamp pairing was guaranteed, or one that would appear if
that pairing ever regressed (a closed duplicate/invalid or an answered
question is also `triage:done` with no bucket, but `--state open` already
excludes it, so it is never a candidate to re-pick up). That one case is
picked back up and bucketed normally rather than left orphaned. CI's Step 1
item 1 (`prompts/orchestrator.md:47-64`, unchanged by this pass) still
requires `triage:done` together with a bucket label as its own skip
condition. That is not the same blind spot local had: Step 1 lists issues
itself before classifying them, so — unlike local's classification prompt —
it does see the labels it is skipping on, and it doubles as CI's dispatch
selector (its `agent-ready`/`approved-for-agent` exception re-enters the
normal lane on every run so an already-approved issue keeps getting
implemented), a job local's `agent triage` does not do. Converging Step 1's
skip wording onto the same bucket-alone rule is tracked under #171 rather
than folded into this pass, which scoped its code change to the local lane
the reviewed regression was found in.

Local additionally skips `agent:claimed` issues outright before they ever
reach the prompt, in the same `src/agent_ops/workflows/triage.py`
(`run_triage`) query: it holds none of the tools (`gh api .../events`,
`gh issue edit`) the shared prompt's stale-claim procedure needs, so leaving
that call to a read-only classification could stamp `needs-human` +
`triage:done` over an issue a run is actively implementing. CI still hands
`agent:claimed` issues to the prompt, since it has those tools and can act on
the stale-claim result itself. `agent status --pipeline` counts how many open
issues carry each of these stage labels, fleet-wide, and how long the oldest
one in each stage has sat there (issue #227) — the thing this table of labels
doesn't show on its own.

**implement.** Local `src/agent_ops/workflows/implement.py` (`run_implement`)
works in an isolated git worktree, runs a plan stage, then a gate loop that
retries up to `loop.max_attempts` (default 3, `src/agent_ops/config.py` (`LoopConfig.max_attempts`))
times with a fresh context on failure, plus a coded self-review pass and
claim/release bookkeeping (#131) so two runs can't collide on the same issue.
The automatic CI implement path is still a single subagent inside the Actions workspace
(`prompts/orchestrator.md` Step 2A item 2, line 82, role file
`prompts/agents/implementer.md`): no worktree, no coded gate loop — the only
retry is the orchestrator's one prose-driven revision round after a Tester
FAIL (Step 2A, "Failure handling", lines 101-109). The dispatch-only hybrid
lane described below is converged with local; it does not replace this
automatic path yet.

**merge — partially converged.** This row differs from the issue that
prompted this page: #150 ("CI lane and `agent merge` now disagree on what a
cap counts") is CLOSED, and the fix (ADR
[`docs/adr/0005-one-merge-cap-evaluator.md`](../adr/0005-one-merge-cap-evaluator.md))
converged the *cap evaluation* itself. Diff-size and blocked-path caps are now
judged by one function, `src/agent_ops/workflows/merge.py` (`evaluate_merge`),
on both sides: local `agent merge` calls it via
`src/agent_ops/workflows/merge.py` (`run_merge`), and CI Step 3
(`prompts/orchestrator.md:144`) shells out to
`uv run --project agent-ops agent merge <PR> --project target --check`, which
is `src/agent_ops/workflows/merge.py` (`run_merge_check`) wrapping the same
`evaluate_merge`. What is still diverged is the merge *decision and action*:
on CI that remains orchestrator prose (`prompts/orchestrator.md:156-167`) —
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
(`src/agent_ops/cli.py` (`review`)), which runs the exact same `run_review` /
`verdict_of` code path as the local lane, honours its exit code and printed
`VERDICT:` line, and feeds that into both the Step 2A revision-round rule and
the Step 3 merge gate (`prompts/orchestrator.md:85-96,104-109,160-161`).
`prompts/agents/reviewer.md` was also anchored to require the same `VERDICT:`
line, since it is still the role file Step 2B's hotfix lane spawns as a
subagent — that lane is unconverged and out of scope for #171 (see below for
what converting Step 2A's item 4 did to Step 2B's reference to that file).

Converting Step 2A's item 4 from a subagent to a shelled-out check removed the
orchestrator's only other reference to `prompts/agents/reviewer.md`, so Step
2B's "run the same four agents" (which named no agents) stopped resolving to
anything — the hotfix lane's review step had gone undefined. Step 2B item 2
(`prompts/orchestrator.md:120-121`) now names all four agents explicitly,
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
CI Step 3 calls. `src/agent_ops/registry.py` (`REGISTRY_FILE`) reads only
`config/local/repos.yml` (a git-ignored, bare list of managed repos), never
`config/repos.yml`.

`auto_merge` as a name survives in two unrelated, still-live places: the
`auto_merge` `workflow_call` input on the triage pipeline (report-only merge
toggle, `.github/workflows/triage-pipeline.yml`, consumed at line 222 as
`AUTO_MERGE` in the orchestrator prompt), and `loop.auto_merge`
(`src/agent_ops/config.py` (`LoopConfig.auto_merge`), default `False`) for
the local implement lane, checked inside
`src/agent_ops/workflows/implement.py` (`run_implement`) to decide whether a freshly opened
PR is immediately run through `run_merge`.

## Trust model

Every local-lane prompt reads GitHub content — issue bodies, comments, PR
descriptions, review threads, CI logs, diffs — that anyone who can open an
issue or comment on a managed repo can write, and some of it (dependency-bot
PR bodies, CI output) no human writes at all.
`src/agent_ops/prompts.py` (`render_task`) prepends the same untrusted-data guard
(`prompts/untrusted-data.md`) to every `prompts/tasks/*.md` template before
it reaches a model: the prompt template and the target repo's `AGENTS.md` /
`CLAUDE.md` are authoritative, everything else is data to reason about, and
an agent that notices injected instructions says so rather than silently
following or ignoring them. `scout`'s repo-focus block
(`src/agent_ops/workflows/scout.py` (`focus_block`), #140) is the trusted end
of the same spectrum: repo-authored text a maintainer configures, held to the
same authority as `AGENTS.md`.

`prompts/orchestrator.md` (the CI lane) does not yet carry this guard — it
renders straight off disk (`.github/workflows/triage-pipeline.yml`), not
through `render_task`, and is a human-reviewed danger zone held by the #171
convergence work. Tracked as a follow-up rather than folded into this file.

## What happens next

- [#171](https://github.com/jirathip-k/agent-ops/issues/171) — converge
  triage, review, and implement onto one implementation instead of two. Review
  flipped first; triage's classification definition converged in #257, though
  its CI wrapper and issue-write actions still diverge (see the triage row
  above); implement remains "no."
- [#150](https://github.com/jirathip-k/agent-ops/issues/150) — closed; the
  merge-cap convergence documented in the merge row above.
- [#139](https://github.com/jirathip-k/agent-ops/issues/139) — open, not yet
  landed: an encyclopedia mapping platform vocabulary to source. Linked here
  as the issue, not as a page, until it lands.

## Footnote: a ninth lane

`promote` also runs on both surfaces and is already fully converged: local
`agent promote` (`src/agent_ops/cli.py` (`promote`)) and
`.github/workflows/promote-pipeline.yml` both open the staging → stable
promotion PR through the same code path. It is not in the table above because
the issue that requested this page scoped it to the eight lanes with a
CI-prompt divergence risk; `promote` never had one.

## Footnote: a tenth lane, dispatch-only by decision

`distill` also runs on both surfaces through one code path: local `agent
distill` (`src/agent_ops/cli.py` (`distill`)) → `src/agent_ops/workflows/distill.py` (`run_distill`),
and CI's `uv run agent distill` (`.github/workflows/distill-pipeline.yml:132`) via
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

## Footnote: an eleventh lane, deliberately reduced scope ([#262](https://github.com/jirathip-k/agent-ops/issues/262))

`classify` runs on both surfaces through the exact same code path as
`triage`'s row in the table above: local `agent triage`
(`src/agent_ops/cli.py` (`triage`)) → `src/agent_ops/workflows/triage.py`
(`run_triage`), and CI's `uv run agent triage`
(`.github/workflows/classify-pipeline.yml:118`) via
`stubs/managed-repo-classify.yml`. It is not in the table above for the same
reason `promote` and `distill` aren't — no CI-prompt divergence risk to
track, since it runs no prompt of its own at all.

It exists because `triage-pipeline.yml`'s own classification step
(Step 1/2 in the table above) shares a concurrency lock and a 55-minute
budget with that pipeline's four-agent implement step, so a long implement
run holds the lock and the next triage tick queues behind it — `untriaged`
ages even while the pipeline is "running". `classify-pipeline.yml` runs only
`agent triage`, in its own `agent-classify-<repo>` lock, so it can no longer
be starved by implement work.

Its scope is deliberately smaller than `triage-pipeline.yml`'s: `run_triage`
applies a bucket label, the `triage:done` stamp, and a reason comment, and
nothing else. It does not close duplicates, answer verifiable questions, or
clear a stale `agent:claimed` label — the local lane's prompt invocation
grants only `gh issue create/list` and `gh search issues`, the same
restriction described in the triage row above for `src/agent_ops/workflows/triage.py`
(`run_triage`), so there is nothing in `run_triage` that could perform those
actions even if `classify-pipeline.yml` wanted it to. Those stay exactly
where they already work, in `triage-pipeline.yml`'s Step 1/2.

This does not starve `triage-pipeline.yml` of the issues `classify` buckets
first: `agent-ready`/`approved-for-agent` overrides `triage:done` and
`backlog` (`prompts/orchestrator.md:61-63`, mirrored in
`triage-pipeline.yml:97-101`'s own precheck), so a bucketed, human-approved
issue re-enters Step 1/2's normal routing on the next triage tick regardless
of which lane bucketed it.

Running `classify` in its own lock is an accepted trade, not a free lunch:
every lane in the `agent-triage-<repo>` group (groom, scout, spec, plan,
triage) can now run concurrently with it, where before they were serialized
by that shared group. `run_groom` (`src/agent_ops/workflows/groom.py`
(`run_groom`)) refreshes stale buckets from a snapshot taken at the start of
its own run, so an issue `classify` buckets mid-groom-run can briefly carry
two labels until the next tick of either lane reconciles it.

`stubs/managed-repo-classify.yml`'s cron (`17 2-3,5-7,9-11,13-15,17-19,21-23
* * *`) is a deliberate deviation from "hourly": a plain hourly schedule fires
inside a live `triage-pipeline.yml` window (`0 */4 * * *`, live up to :55) six
times a day, so the six hours triage occupies are excluded outright, along
with groom's hour (`0 1 * * *`). The goal `classify` exists for —
`untriaged` no longer ageing for days — only needs a gap that never exceeds
two hours; preserving literal hourliness was not worth reintroducing that
collision for.

Two overlaps remain, neither engineered around:

- **Scout (18:00, 30 min), spec (19:00), and plan (19:20)** can still
  overlap classify at 18:17 and 19:17. None of the three applies a bucket
  label, so there is no path to a divergent verdict — this is a scheduling
  coincidence, not a race on shared state.
- **The `agent:claimed` TOCTOU window.** An implement run can claim an issue
  after `classify`'s `gh issue list` has already returned, at any hour — no
  cron arrangement closes this, since it isn't a collision between two
  scheduled lanes. The accepted consequence is a bucket label and
  `triage:done` stamped on an issue that is, by the time the comment posts,
  already claimed and being worked; the next `run_groom` tick reconciles it
  the same way it reconciles the groom-vs-classify race above. Worth a
  follow-up issue if it proves to matter in practice, not a reason to add a
  claim-state guard to this lane.

## Footnote: a twelfth lane, manual hybrid implementation ([#296](https://github.com/jirathip-k/agent-ops/issues/296))

`implement` now has a second CI surface that runs the local workflow unchanged:
`uv run agent implement "$ISSUE" -C "$GITHUB_WORKSPACE/target"`
(`.github/workflows/implement-pipeline.yml:138`) via
`stubs/managed-repo-implement.yml`. It deliberately passes no `--runtime`.
The target repo's `.agent/config.yaml` therefore resolves each role
independently; a Claude planner, Codex implementer, and Claude reviewer use
their own configured model tiers through `ProjectConfig.resolve_role`.

The caller is `workflow_dispatch` only and its numeric `issue` input is
required (`stubs/managed-repo-implement.yml:14-20`). It has no schedule and
does not scan `agent-ready`, so the first release cannot consume arbitrary
backlog work. It also shares `agent-triage-<repo>` with the existing automatic
implementation path. This is intentionally conservative while real hybrid
runs establish whether the lane is ready to take automatic queue ownership.

Both provider credentials are isolated from target-controlled setup and gate
subprocesses. `openai/codex-action` receives the raw OpenAI key, drops sudo,
starts its Responses API proxy, and writes a proxy-backed Codex home
(`.github/workflows/implement-pipeline.yml:109-117`); agent-ops never receives
the raw key. The Claude token exists in environment only in the final trusted
shell step, which writes it to a mode-0600 runner-temporary file and unsets the
variable before launching agent-ops (`.github/workflows/implement-pipeline.yml:123-138`).
`capture_ci_credentials` consumes and unlinks that file, removes both carrier
variables before target setup begins, and the runtime adapters restore only
their own credential to their own CLI child. Repository setup and gates
therefore inherit neither the Anthropic token nor the proxy-backed
`CODEX_HOME`.

Before implementation, `agent runtime-preflight` resolves all three roles and
requires every runtime CLI. Missing Claude/Codex executables and missing model
tier mappings fail with role-named diagnostics
(`src/agent_ops/cli.py` (`runtime_preflight`)). A trusted credential-validation
step separately names a missing `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`
before either provider is invoked. The setup contract for a managed repo is
therefore two repository secrets plus complete per-runtime tier tables for
every tier its configured roles select.

## Footnote: CI-lane commit identity ([#203](https://github.com/jirathip-k/agent-ops/issues/203))

`distill` and `evolve` are the first CLI-lane pipelines to commit to a
branch — GitHub-hosted runners configure neither `user.name` nor
`user.email`, so a plain `git commit` there fails only after the run has
already paid for checkout, setup, and a full agent session. The fix lives in
code, not YAML: `src/agent_ops/worktree.py` (`commit`) falls back to
a `github-actions[bot]` identity only when git has none configured anywhere
(global, system, repo, or env) — a local `agent distill` / `agent evolve`
still commits as the developer. `implement.py`'s `_finish_run` uses the same
helper. A `git commit` shelled out anywhere else fails
`tests/test_commit_identity_drift.py`. The `git config --global` steps
already in `distill-pipeline.yml` and `evolve-pipeline.yml` are redundant
with this fallback but kept — removing them is a workflow-YAML edit outside
this fix's scope.

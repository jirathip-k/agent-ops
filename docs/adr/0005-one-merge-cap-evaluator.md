# ADR 0005: One merge-cap evaluator, shared by the CI lane and `agent merge`

**Status:** accepted · 2026-07-27

## Context

#136 taught `evaluate_merge` (`src/agent_ops/workflows/merge.py`) to size a
PR on non-test lines/files, with a raw-total backstop covering everything.
The CI lane never called that function: its Step 3 gate was prose in
`prompts/orchestrator.md` ("Diff ≤ 200 changed lines and ≤ 5 files"),
judged by the orchestrator model against numbers in `config/repos.yml`
that nothing in the code path actually read (`registry.py` reads
`config/local/repos.yml` instead; `config/repos.yml`'s `auto_merge` block
was dead).

So after #136 the two lanes attached different meanings to caps that read
the same: local `agent merge` counted non-test lines/files with a 4x raw
backstop; the CI lane counted every line and file, unbackstopped, evaluated
by a model rather than tested code. A PR of 150 production + 300 test lines
auto-merged locally and was refused by the CI lane, and neither result was
wrong by its own rules — a silent semantics divergence (issue #150).

## Decision

Teach the CI lane the local lane's semantics instead of maintaining a
second implementation (option 1 of the three sketched in #150):

- `run_merge_check()` (`workflows/merge.py`) wraps `evaluate_merge` as a
  check-only entry point: fetch the PR, evaluate, print each violation (or
  an explicit "no violations" line), merge nothing.
- `agent merge <PR> --check` exposes it — exit 0 on a clean verdict, exit 1
  on any violation; `--check --override` is a rejected combination, not a
  silent no-op.
- `prompts/orchestrator.md` Step 3 replaces the two numeric/path prose
  bullets with an instruction to run
  `uv run --project agent-ops agent merge <PR> --project target --check`
  and honour its verdict verbatim, quoting its violation lines rather than
  re-deriving them.
- `config/repos.yml`'s dead `auto_merge` block is deleted; a comment points
  at the target repo's `.agent/config.yaml` (`merge:` section) as the one
  place caps and blocked paths are actually configured.

CI/CD-green and Tester-PASS/Reviewer-APPROVE stay orchestrator prose — they
are not `evaluate_merge`'s job. One prose bullet remains deliberately: infra
files outside the coded `blocked_paths` list (`*.tfvars`,
`docker-compose.yml`, Helm charts) still block via the orchestrator's
judgment, since `--check` does not enumerate every possible infra pattern
and the prior prose was open-ended there on purpose.

## Consequences

- One tested implementation of "does this PR fit the caps" — `evaluate_merge`
  — instead of two that can silently drift again. Parity between the CI
  lane and `agent merge` is now a property of shared code, not a hope, and
  is pinned by a test (`tests/test_merge_rules.py`) that runs the #150
  worked example (150 production + 300 test lines) through `--check`.
- The CI lane's effective caps change: 200 raw lines / 5 raw files becomes
  400 production lines / 12 production files with a 1600/48 raw backstop
  (the target repo's configured defaults). This is the intended effect of
  fixing the divergence, not a side effect — a PR shaped like the mixed
  example above now merges instead of being refused.
- If `uv run --project agent-ops agent merge ... --check` fails to execute
  (network, `uv` resolution), the orchestrator has no verdict and must treat
  that as blocking rather than merging past it.
- Two declared danger zones (`prompts/orchestrator.md`,
  `config/repos.yml`) are edited by this change under the explicit,
  scoped authorization in #150 — human review required before it lands.

## Rejected alternatives

- **Align the numbers and document the difference (option 2).** Cheaper,
  but leaves two implementations that can drift again the next time either
  side's caps or exclusions change — the same failure mode #150 exists to
  close, just with better docs.
- **Accept and document only (option 3).** Records the divergence without
  removing it; the CI lane's prose caps would remain untested and
  independently maintained indefinitely.

# Implementation Notes — #64: dedupe bare `#N` mention no longer blocks

## What changed

- `src/agent_ops/github.py`
  - `open_prs_for_issue`: widened the existing single `gh pr list --json ...`
    call to also request `closingIssuesReferences`. No new `gh` invocation
    was added — this is the same call that already ran, with one more field.
  - `pr_references_issue`: rewritten to drop the bare `#N` regex entirely.
    It now returns `True` only if:
    1. `headRefName == "fix/issue-{issue_number}"` (unchanged), or
    2. `pr.get("closingIssuesReferences") or []` contains an entry whose
       `"number"` equals `issue_number` (new — this is GitHub's own
       closing-keyword parser, surfaced via the API field, not a local
       regex).
  - Removed the now-dead `import re` (line 4) since nothing in the module
    uses `re` anymore.
- `tests/test_github.py`
  - Inverted the two tests that previously asserted the buggy behavior
    (`test_pr_references_issue_matches_body_mention`,
    `test_pr_references_issue_matches_title_mention`) into negative tests:
    `test_pr_references_issue_does_not_match_bare_body_mention` and
    `test_pr_references_issue_does_not_match_bare_title_mention`, each with
    an empty `closingIssuesReferences` list.
  - Added the issue's own regression fixture (sendmeter #197/#194 text):
    `test_pr_references_issue_regression_sendmeter_bare_mention_does_not_block`
    and `..._real_closing_reference_blocks`, proving the bare cross-reference
    to #194 does not block while the PR's real "Fixes #189" (via
    `closingIssuesReferences`) still does.
  - Added `test_pr_references_issue_matches_closing_reference` to verify the
    new field alone (independent of title/body text) drives a match.
  - Kept `test_pr_references_issue_matches_branch_name` unchanged.
  - Rewrote `test_pr_references_issue_does_not_match_longer_number` /
    `..._does_not_match_shorter_number` to use `closingIssuesReferences`
    entries (`number: 1321` / `number: 13`) instead of text, proving no
    accidental match on the new path either.
  - Renamed `test_pr_references_issue_handles_missing_body` to
    `test_pr_references_issue_handles_missing_closing_references_field`,
    covering a PR dict with no `closingIssuesReferences` key at all (must
    not raise, must return `False` unless the branch matches).
  - Added `test_open_prs_for_issue_requests_closing_references`, mirroring
    the existing `test_get_issue_requests_comments` pattern, asserting
    `"closingIssuesReferences"` is present in the `--json` field list passed
    to `gh pr list`.
  - Extended `test_open_prs_for_issue_filters_to_matching_prs` with a third
    fixture PR (#142) that has an unrelated branch name and only a bare
    `#132` mention in its body, but a real `closingIssuesReferences` entry
    for 132 — proving the filter follows the new field, not text, and that
    a bare mention alone (PR #141, which has neither) is still excluded.
  - `test_open_prs_for_issue_returns_empty_when_gh_fails` left unchanged
    (fail-open behavior is untouched).
- `docs/workflow.md` (~line 136-140): updated the prose describing the
  dedupe match from "a `#N` mention in the title/body" to a real GitHub
  closing reference (`Fixes`/`Closes`/`Resolves #N`, as GitHub parses it) or
  the `fix/issue-N` branch name, explicitly noting a bare mention elsewhere
  does not count.

No changes were made to `src/agent_ops/cli.py` or
`src/agent_ops/workflows/implement.py` — per the plan, they only call
`open_prs_for_issue`, they don't inspect PR shape themselves, and their own
tests (`tests/test_cli_dispatch.py`, `tests/test_implement_workflow.py`)
already monkeypatch `open_prs_for_issue` directly, so they were unaffected.

## Why

`pr_references_issue` previously matched any bare `#N` substring anywhere in
a PR's title/body, so a PR that merely cross-referenced a related-but-out-
of-scope issue (e.g. "the root cause is tracked separately in #194 and is
out of scope here") was wrongly treated as already fixing issue #194,
permanently blocking `agent implement 194` (and the equivalent CI-lane
dispatch) from ever running. The fix asks GitHub itself (via
`closingIssuesReferences`, which GitHub populates by parsing
`Fixes`/`Closes`/`Resolves #N` keywords in the PR body) instead of
re-implementing that keyword parsing locally, and does so without adding a
new network call — it widens the one `gh pr list` call `open_prs_for_issue`
already made.

## Deviations from the plan

1. **Test naming**: `test_pr_references_issue_handles_missing_body` was
   renamed to `test_pr_references_issue_handles_missing_closing_references_field`
   to accurately describe what it now covers (a missing
   `closingIssuesReferences` key, not a missing `body`). The plan explicitly
   called this "generalizing" the existing test's intent; renaming was not
   spelled out verbatim but is a natural consequence of that generalization
   and involves no behavior change.
2. **Local test-gate verification could not be completed**: the sandbox this
   implementation ran in has no `uv`, `pytest`, `ruff`, or `pyright`
   installed, and both package installation (`pip`, `uv`, `curl`-based
   installers) and arbitrary `python3 -m <module>` invocations are blocked
   by the environment's permission system with no interactive approver
   available in this session. I was therefore unable to run `uv run pytest
   -q`, `uv run ruff check . && uv run ruff format --check .`, or `uv run
   pyright` locally as instructed in step 5.
   - I mitigated this with careful manual review: traced every test case in
     the new/edited `tests/test_github.py` by hand against the new
     `pr_references_issue`/`open_prs_for_issue` implementations line by
     line, confirmed no other use of `re` remains in `github.py` (so the
     import removal is safe), and checked line lengths in all three changed
     files stay under the 100-character limit (`wc -L`: 96 / 94 / 82).
   - This is an environment limitation, not a plan defect — the plan itself
     was fully implementable and was implemented as specified. I recommend
     this PR's CI run (which does have the full toolchain) be treated as the
     first real confirmation that the gates pass, and that a human/reviewer
     double-check the CI result before merge.

No other deviations. The change is scoped exactly to `github.py`,
`tests/test_github.py`, and `docs/workflow.md`, with no refactors,
dependency changes, or scope creep beyond the plan.

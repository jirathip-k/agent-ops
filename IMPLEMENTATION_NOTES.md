# IMPLEMENTATION_NOTES — issue #87

PR: https://github.com/jirathip-k/agent-ops/pull/93
Branch: `fix/issue-87` → base `staging`

## What changed

`agent runs` used to derive an issue's state purely from live signals
(worktree, process table, halt feedback file, open PR) — all four of which
`_finish_run` clears or consumes on the success path, so a successfully
finished run vanished from `agent runs` instead of showing `done`. This adds a
durable outcome record on the way out and reads it back in `discover_runs`.

### `src/agent_ops/workflows/implement.py` (write side)

- Added `import json` and `import time`.
- `_outcome_path(project_root, issue_number)`, next to `_ad_hoc_message_path`,
  following the same convention.
- `_write_outcome(project_root, issue_number, *, state, pr_url, reason, log)`:
  writes `{"state", "pr_url", "reason", "finished_at"}` as JSON via
  temp-file-then-`Path.replace` (already an atomic rename, so no `os` import),
  wrapped in `try/except OSError` that logs and swallows — mirrors
  `_record_halt`'s best-effort philosophy.
- `_clear_outcome(project_root, issue_number, *, log)`: `unlink(missing_ok=True)`
  behind the same best-effort `try/except OSError`. See "Review round" below
  for why it exists and where it is called.
- In `_finish_run`: hoisted `pr_url: str | None = None` before the `if open_pr:`
  block, set `pr_url = url` right after `github.create_pr(...)`, and — after
  the PR/auto-merge block, before the feedback-file cleanup — added
  `_write_outcome(..., state="done", pr_url=pr_url, reason=None, log=log)`.
  Only the success path writes a record (`state="done"` always); the
  FAILED-gates/`stopped` path is a deliberate deferral.

### `src/agent_ops/runs.py` (read side)

- Added `import json`, `_OUTCOME_RE`, `_OUTCOME_TTL_S = 7 * 24 * 3600.0`, and
  a frozen `Outcome` dataclass (`state`, `pr_url`, `reason`).
- `_outcome_detail(outcome)`: renders `PR #{n} — {url}` when a PR url parses,
  `reason or "(recorded)"` otherwise.
- `_load_outcomes(run_files, now, log)`: parses every `issue-N-outcome.json`,
  pruning (via `unlink(missing_ok=True)`) anything whose file mtime is older
  than `_OUTCOME_TTL_S` — keyed on file mtime rather than the JSON's own
  `finished_at` so a corrupt file still ages out. Malformed JSON / missing
  keys / stat errors log a `warning:`-prefixed message and are skipped, never
  raised.
- `classify()`: added `outcome: Outcome | None = None` keyword parameter
  (default preserves every existing call site unmodified). Precedence is now
  `live > outcome > has_feedback > pr > worktree > None` — an outcome record
  outranks a stale feedback file, which is issue #78's "reports halted
  forever" symptom.
- `discover_runs()`: loads outcomes from the same `run_files` listing used for
  feedback/log candidates, adds `set(outcomes)` to the `candidates` union, and
  passes `outcome=outcomes.get(issue)` into `classify(...)`.
- No changes to `wait_for_runs` — `TERMINAL_STATES` already includes `"done"`.

## Rebase onto the new `staging` (PR #89)

PR #89 landed on `staging` while this branch was open and touched the same
function. It changed `discover_runs`'s return type from `list[Run]` to
`tuple[list[Run], bool]`, where the `bool` ("trustworthy") is `False` whenever
`worktree.list_worktrees` or `github.open_prs` raised `CommandError` and had
to be degraded to an empty list; `wait_for_runs` was reworked around it
(`_MAX_DEGRADED_POLLS`, degraded-poll streak handling, never reading `gone`
off a degraded poll).

Two real conflicts, resolved to keep **both** behaviours:

1. **Module constants.** Both sides appended to the same block. Kept
   `_MAX_DEGRADED_POLLS = 4` (#89) *and* `_OUTCOME_TTL_S` (this PR).
2. **`discover_runs` signature + preamble.** Took #89's `tuple[list[Run], bool]`
   signature, docstring and `trustworthy` bookkeeping wholesale, and kept this
   PR's `_load_outcomes` helper (which #89 never saw) immediately above it.
   The function body auto-merged cleanly: #89's `try/except` around
   `list_worktrees` and its two `trustworthy = False` assignments sit alongside
   this PR's `outcomes = _load_outcomes(...)`, the `| set(outcomes)` term in
   `candidates`, and `outcome=outcomes.get(issue)` in the `classify(...)` call.
   Both `return` statements are now the tuple form (`return [], trustworthy` /
   `return runs, trustworthy`).

Deliberate resolution detail: **outcome records never make a poll
untrustworthy.** `trustworthy` means "this poll's `gh`/`git` signals can be
believed"; outcome records are read straight off local `.agent-runs/` with no
subprocess that could degrade. A corrupt or unreadable record logs a warning
and is skipped, but must not push `wait_for_runs` toward its degraded-streak
bail-out — that error is about an outage, not about a bad local file. This is
documented in `discover_runs`'s docstring and asserted by two tests
(`assert trustworthy is True` in the outcome-only and invalid-JSON cases).

Test updates from both sides: the four outcome tests this PR added to
`tests/test_runs.py` and the end-to-end one in `tests/test_resume.py` now
unpack the tuple. #89's own tests (including its `_polls` helper, which already
marks each round trustworthy) needed no changes.

## Review round — PR #93 review comment

Reported at
https://github.com/jirathip-k/agent-ops/pull/93#issuecomment-5081227651.

`classify` ranks `outcome` above `has_feedback`, but nothing ever cleared
`issue-N-outcome.json` when a *new* cycle started on the same issue. Within the
7-day TTL:

1. `agent implement 50` succeeds → `_finish_run` writes `outcome.json {done}`
2. issue reopened, a later run on #50 halts at self-review → `_record_halt`
   writes `issue-50-feedback.md`
3. `agent runs` reports `done  PR #NN` — the halt is invisible and the user is
   never told to `agent resume 50`

The `outcome > feedback` precedence itself is correct and is kept: it is what
stops a stale halt file from reporting `halted` forever (#78). What was missing
is that the precedence is only sound while the record describes the *latest*
cycle. Fix is on the write side, in `_clear_outcome`:

- **`_record_halt`** clears the record before writing the feedback file. Being
  shadowed is most harmful here — it is the one exit that needs a human.
- **The start of a run clears it too** (`run_implement` and `run_resume`). The
  reviewer left this optional ("and/or"); I took it, because `_record_halt`
  alone leaves the same defect on a different exit path. The gate-failure exit
  writes *no* feedback file and *no* record of its own — it relies on the kept
  worktree classifying as `stopped`. A stale `done` shadows that just as
  completely, so a genuinely failed run reads as finished. Clearing when the
  cycle starts covers every exit at once (halted, stopped, done, running)
  rather than one at a time, and it is the honest statement anyway: from the
  moment a new cycle owns the issue, the previous cycle's verdict is no longer
  the current word.

Placement of the two start-of-run calls is deliberate:

- `run_implement`: **after** the "issue already has an open PR" bail-out. That
  path starts no cycle and returns `False`, so it has no business discarding
  the previous cycle's record. Covered by
  `test_an_open_pr_bail_out_leaves_the_outcome_record_alone`.
- `run_resume`: **after** `_resolve_feedback`, which raises when there is
  nothing to resume from — again, no cycle started, nothing to supersede.

`_record_halt` clears it a second time even though the start of the run already
did. That is intentional redundancy, not an oversight: it is one `unlink` on a
path that must not be wrong, and it keeps the guarantee local to the function
whose file would otherwise be shadowed.

Failure mode: `_clear_outcome` is best-effort like everything else in
`_record_halt`. An `OSError` logs `could not clear stale outcome record at …`
and the halt still records — a status file that will not delete must never
crash a halt. Failing to delete only leaves pre-existing staleness in place.

### Tests

- `tests/test_resume.py`:
  - `test_a_new_cycles_halt_supersedes_the_previous_cycles_outcome_record` —
    the regression test the review asked for. Writes a `done` record for #11,
    runs a full `run_implement` that halts at self-review, then asserts
    `discover_runs` reports `halted` with `agent resume 11` in the detail, and
    that the record is gone.
  - `test_starting_a_new_run_clears_the_previous_cycles_outcome_record` — the
    gate-failure path: stale `done` must not mask `stopped`.
  - `test_record_halt_clears_the_outcome_record_even_when_the_unlink_fails` —
    monkeypatches `Path.unlink` to raise; the halt is still recorded and the
    failure is logged.
  - `test_an_open_pr_bail_out_leaves_the_outcome_record_alone` — guards the
    placement decision above.
- `tests/test_runs.py`:
  `test_classify_outcome_over_feedback_is_only_safe_because_a_halt_clears_it`
  pins the invariant at the `classify` level and points at the write-side test,
  so a future reader sees why the precedence is safe rather than just that it
  exists.

Verified the tests actually catch the defect: with the three `_clear_outcome`
call sites stubbed out, `test_a_new_cycles_halt_supersedes_…`,
`test_starting_a_new_run_clears_…` and `test_record_halt_clears_…` all fail;
they pass with the fix in place.

## Gate results

Run locally in this worktree, on the rebased branch, with the fix in place:

- `uv run pytest -q` — **439 passed**
- `uv run ruff check . && uv run ruff format --check .` — **All checks passed!
  / 59 files already formatted**
- `uv run pyright` — **0 errors, 0 warnings, 0 informations**

(Earlier revisions of this file claimed the gates could not be executed in the
sandbox. That claim is withdrawn: `uv run` works here and all three gates were
actually run for this round.)

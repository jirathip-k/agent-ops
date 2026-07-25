# IMPLEMENTATION_NOTES: issue #86

PR: https://github.com/jirathip-k/agent-ops/pull/89
Branch: `fix/issue-86` → base `staging`

## What changed

`src/agent_ops/runs.py` (plus `tests/test_runs.py`):

1. Added `_MAX_DEGRADED_POLLS = 4` module constant next to `_POLL_INTERVAL_S`.
2. `discover_runs`'s return type changed from `list[Run]` to
   `tuple[list[Run], bool]`. The `bool` ("trustworthy") is `False` whenever
   either `worktree.list_worktrees` or `github.open_prs` raised
   `CommandError` this poll. `worktree.list_worktrees` is now wrapped in a
   try/except that degrades to an empty worktree list and logs a warning —
   mirroring the existing `github.open_prs` try/except — instead of letting
   `CommandError` propagate straight out of `discover_runs` (previously a
   transient `git worktree list` hiccup could kill an hour-long `--wait`
   with exit 1).
3. `report_runs` unpacks and ignores the new flag (one-shot snapshot; the
   warning is already logged by `discover_runs`).
4. `wait_for_runs`:
   - Captures the raw text of the latest `warning:` message via a small
     `capture()` wrapper placed *before* `_dedup_warnings`, so the
     degraded-streak error message can name the last warning even if
     `_dedup_warnings` has since suppressed it from the printed output.
   - Tracks `degraded_streak` (consecutive untrustworthy polls, reset to 0 on
     any trustworthy poll) and `degraded_polls`/`total_polls` (for the final
     summary).
   - Raises `CommandError` once `degraded_streak > _MAX_DEGRADED_POLLS`,
     naming the last warning seen. `cli.py` already catches `CommandError`
     from this call and exits 1 with the message — no CLI change needed, as
     the plan anticipated.
   - On an untrustworthy poll: a watched issue missing from `found` is
     *not* transitioned to `gone` — its previous state and `stopped_streak`
     are carried forward unchanged (the `continue` branch), and no
     transition line is logged for it. A watched issue still present in
     `found` on an untrustworthy poll has its `stopped_streak` forced to 0
     rather than incremented, so a degraded `stopped` observation can never
     itself reach the 2-poll debounce threshold; other state changes (e.g.
     genuine `done`/`halted` positive evidence) still log/update normally.
   - On a clean terminal return, if any poll during the wait was degraded,
     logs one final `note: PR/worktree data was unreliable for N of M polls
     in this wait` line before returning.

## Why

Exactly per `PLAN.md`'s root-cause analysis: a `gh` outage or worktree-list
hiccup produced output indistinguishable from a genuinely stopped/gone run,
and the existing 2-poll `stopped` debounce and 0-poll `gone` transition had
no way to discount a poll known to be degraded.

## Test additions (`tests/test_runs.py`)

- `test_discover_runs_worktree_listing_failure_still_yields_rows` — new
  worktree-failure case, mirroring the existing `gh`-failure test.
- `test_wait_for_runs_single_degraded_poll_resets_stopped_streak`
- `test_wait_for_runs_raises_after_sustained_gh_outage`
- `test_wait_for_runs_worktree_listing_failure_mid_wait_recovers`
- `test_wait_for_runs_untrustworthy_disappearance_is_not_gone`
- `test_wait_for_runs_logs_degradation_summary_on_clean_finish`

Existing tests updated only for the new tuple return shape (`_polls` helper,
the inline `discover_runs` tests, and the inline `fake` in
`test_wait_for_runs_dedups_repeated_warning`); no behavior assertions in the
pre-existing tests were changed, and the two regression tests named in the
plan (`..._requires_two_consecutive_polls`, `..._dedups_repeated_warning`)
are unmodified in intent.

## Deviations from the plan

None in substance. One clarification made while implementing: the plan's
prose bundles "reset stopped_streak to 0" and "don't transition to gone"
into one sentence about untrustworthy polls; I read this as two related but
separable behaviors and implemented both — the streak reset applies to
every watched issue on an untrustworthy poll (whether or not it's still
present in `found`), while the "hold previous state, no gone transition" only
applies to the specific case of an issue missing from `found`. This matches
every acceptance-criteria scenario listed in the plan's test plan section.

## Gates

`uv run pytest -q`, `uv run ruff check . && uv run ruff format --check .`,
and `uv run pyright` could **not** be run in this sandboxed environment:
`uv`, `pip3`, `pipx`, `ruff`, `uvx`, and even bare `python3 -m ...` /
`python3 -c ...` invocations all require interactive approval that this
automated pipeline has no channel to grant (only `git`, `gh`, and plain
read-only shell utilities are usable here). This is an environment
limitation, not a decision to skip the gates.

Mitigations applied instead:
- Full manual re-read of the resulting `runs.py` and `test_runs.py` for
  syntax correctness and control-flow tracing (each new test's expected
  poll-by-poll state transitions was hand-traced against the implementation).
- Verified via `grep` that no line in either changed file exceeds the
  configured 100-char ruff line length.
- Verified via `wc -c` that `discover_runs`'s new signature line is exactly
  100 chars so it stays on one line rather than being wrapped in a way `ruff
  format` would then want to collapse back.
- Full type hints added on the new tuple return, the new module constant,
  and every new local (`trustworthy: bool`, `degraded_streak`/`degraded_polls`/
  `total_polls: int`, `last_warning: str | None`).

Recommend the reviewer confirm this PR's own CI run (which does have `uv`
available) is green before merging.

## Revision (round 2, per TEST_REPORT.md FAIL)

The tester found that all of the above protection lived in the `else` branch
of `if watch is None:` in `wait_for_runs` — i.e. only from the *second* poll
onward, once a watch set already existed. The `if watch is None:` branch
itself, which runs exactly once on the very first poll, ignored `trustworthy`
completely:

- `--wait --issue N` with a degraded first poll: if `N` wasn't in `found`
  that round, it immediately raised `CommandError("no run found for #N")`,
  even though the data was untrustworthy.
- `--wait` (watch-all) with a degraded first poll: if `found` was empty, it
  immediately did `capture("no agent runs found"); return True` (exit 0) —
  the exact false-terminal-success shape issue #86 was filed against,
  relocated to the watch-establishment step.

### Fix

In `src/agent_ops/runs.py`'s `wait_for_runs`, the `if watch is None:` branch
now checks `trustworthy` before concluding either verdict:

- Named-issue case: `issue not in found` only raises `CommandError` when
  `trustworthy` is `True`. On an untrustworthy poll, `watch` is simply left
  `None` and the loop proceeds to the next poll — no state to hold since the
  watch set doesn't exist yet.
- Watch-all case: an empty `set(found)` only triggers `capture("no agent
  runs found"); return True` when `trustworthy` is `True`. On an
  untrustworthy poll, `watch` stays `None` and the loop retries.
- Either way, the existing `degraded_streak`/`_MAX_DEGRADED_POLLS` counting
  (already incremented earlier in the same loop iteration, before the
  `if watch is None:` block is reached) still applies unchanged — sustained
  degradation from poll 1 raises the same degraded-outage `CommandError`
  after more than `_MAX_DEGRADED_POLLS` consecutive untrustworthy polls, so
  this can't hang forever on a permanent outage.
- The final `if all(is_terminal(i) for i in watch):` check is now guarded
  with `watch is not None and ...`, since `watch` can legitimately still be
  `None` after a poll that neither established it nor raised/returned.
- Docstring updated to describe "the first *trustworthy* poll" instead of
  simply "the first poll".

No changes outside `wait_for_runs`'s watch-establishment branch and its
guard; `discover_runs`, `classify`, `report_runs`, and the steady-state
(already-watched) branch are untouched.

### Tests added (`tests/test_runs.py`)

- `test_wait_for_runs_first_poll_degraded_named_issue_retries` — degraded
  first poll with the named issue absent, recovers on poll 2, finishes
  `done` on poll 3.
- `test_wait_for_runs_first_poll_degraded_watch_all_retries` — degraded
  first poll with an empty result in watch-all mode, recovers and finishes
  the same way; asserts "no agent runs found" is never logged.
- `test_wait_for_runs_raises_when_named_issue_degraded_from_first_poll` —
  every poll degraded from poll 1 onward, named issue never confirmed:
  raises the degraded-outage `CommandError` (not "no run found").
- `test_wait_for_runs_raises_when_watch_all_degraded_from_first_poll` — same
  for watch-all mode: raises the degraded-outage `CommandError`, never
  "no agent runs found".

All four hand-traced poll-by-poll against the updated implementation.

### Gates

Same environment constraint as the initial round: `uv`, `pip3`, `ruff`, and
even bare `python3 -c "..."` all require interactive approval unavailable in
this sandbox, confirmed again this round (`python3 -c "print(1+1)"` itself
required approval). Verified instead by:
- Full manual re-read and poll-by-poll trace of `wait_for_runs` against all
  four new tests plus every pre-existing `wait_for_runs`/`discover_runs`
  test, confirming the trustworthy-path behavior is byte-for-byte unchanged
  (the refactor into `candidate`/nested `if trustworthy` is equivalent to
  the prior unconditional logic whenever `trustworthy` is `True`, which is
  what every pre-existing test drives).
- `grep -P ".{101,}"` over both changed files: no line exceeds the 100-char
  ruff line length.
- Manual review confirming no Python syntax issue from comment-only
  `if`/`else` bodies (each such branch already contains a preceding
  statement satisfying the block requirement; the comments are trailing,
  not the sole content of an indented block).

Recommend the reviewer confirm PR #89's own CI run is green before merging.

## Revision (round 3, per reviewer REQUEST CHANGES on PR #89)

The reviewer flagged that the watch-establishing code path (the loop under
`if watch is None:` that seeds initial state, `runs.py` around what was then
line 513) seeded `stopped_streak[i] = 1 if r.state == "stopped" else 0` with
no check on `trustworthy` — unlike the steady-state update loop a few lines
below, which explicitly zeroes the streak on an untrustworthy poll.

This mattered because the very first poll establishing the watch can itself
be untrustworthy and coincide with the documented dispatch pre-spawn race
window (worktree exists, child hasn't exec'd yet, reads as `stopped`). That
seeded a bogus streak of 1 from an untrustworthy observation. A single
*subsequent trustworthy* poll that still read `stopped` (the same race still
settling) then pushed the streak to 2 and fired `is_terminal` after only one
trustworthy observation — violating this PR's own documented "two consecutive
trustworthy polls" debounce invariant. No existing test covered this exact
combination (untrustworthy first poll + `stopped` state on the to-be-watched
issue).

### Fix

Applied exactly the reviewer's suggested change in `src/agent_ops/runs.py`,
in the watch-establishing loop:

```python
stopped_streak[i] = 1 if (trustworthy and r.state == "stopped") else 0
```

Now a `stopped` observation only seeds a streak of 1 when the poll that
produced it was trustworthy; an untrustworthy first poll always seeds 0,
regardless of the reported state, matching the steady-state loop's existing
`if not trustworthy: stopped_streak[i] = 0` behavior. This is the only
behavioral change in this round.

### Minor fix (non-blocking note from the same review)

The reviewer also noted, as a non-blocking observation, that the two
early-exit paths in the watch-establishing branch — `raise
CommandError("no run found for #{issue} ...")` and `capture("no agent runs
found"); return True` — didn't surface the degraded-polls summary that the
terminal-return path already logs. Since this was small and contained (no
restructuring), both paths now log the same `note: PR/worktree data was
unreliable for N of M polls in this wait` line (via the existing `capture()`
helper) when `degraded_polls > 0`, before raising/returning. This does not
change the exception message matched by `test_wait_for_runs_unknown_issue`
(`match="no run found for #99"` is a substring search) nor any other existing
assertion.

### Test added (`tests/test_runs.py`)

- `test_wait_for_runs_untrustworthy_first_poll_does_not_seed_stopped_streak`
  — first poll is untrustworthy and reports `stopped` on issue #77 (the exact
  scenario in the reviewer's finding); the next two polls are trustworthy and
  still report `stopped`. Asserts the wait only terminates after the *third*
  poll overall (i.e., after two consecutive *trustworthy* `stopped`
  observations), not the second. Hand-traced against the buggy code, this
  test fails at `calls["n"] == 2` (false-early termination) and passes at
  `calls["n"] == 3` with the fix applied.

### Gates

Same environment constraint as prior rounds: `uv`, `pip3`, `ruff`, and bare
`python3 -c "..."` all require interactive approval unavailable in this
sandbox (confirmed again this round). Verified instead by:
- Full manual poll-by-poll trace of the new test against both the pre-fix and
  post-fix code, confirming it discriminates exactly the bug described.
- `grep -P ".{101,}"` over both changed files: no line exceeds the 100-char
  ruff line length.
- Re-read of all pre-existing `wait_for_runs` tests to confirm the added
  `trustworthy and` guard and the two `degraded_polls` notes do not change
  behavior on any poll where `trustworthy` is `True` (the overwhelming
  majority of existing test polls), since `trustworthy and X` is identical to
  `X` whenever `trustworthy` is `True`.

Recommend the reviewer confirm PR #89's own CI run is green before merging.

# IMPLEMENTATION_NOTES — issue #87

## What changed

`agent runs` used to derive an issue's state purely from live signals
(worktree, process table, halt feedback file, open PR) — all four of which
`_finish_run` clears or consumes on the success path, so a successfully
finished run vanished from `agent runs` instead of showing `done`. This
implements the plan exactly:

### `src/agent_ops/workflows/implement.py` (write side)

- Added `import json` and `import time`.
- Added `_outcome_path(project_root, issue_number)`, next to
  `_ad_hoc_message_path`, following the same convention.
- Added `_write_outcome(project_root, issue_number, *, state, pr_url, reason, log)`:
  writes `{"state", "pr_url", "reason", "finished_at"}` as JSON via
  temp-file-then-`Path.replace` (equivalent to `os.replace`, no `os` import
  needed since `Path.replace` already does an atomic rename), wrapped in
  `try/except OSError` that logs and swallows — mirrors `_record_halt`'s
  best-effort philosophy.
- In `_finish_run`: hoisted `pr_url: str | None = None` before the `if open_pr:`
  block, set `pr_url = url` right after `github.create_pr(...)`, and — after
  the PR/auto-merge block, before the feedback-file cleanup — added the call
  `_write_outcome(project_root, issue_number, state="done", pr_url=pr_url,
  reason=None, log=log)`. Only the success path writes a record (`state=
  "done"` always); the FAILED-gates/`stopped` path is left as a deliberate
  deferral, per the plan's Risk Notes.

### `src/agent_ops/runs.py` (read side)

- Added `import json`, `_OUTCOME_RE`, `_OUTCOME_TTL_S = 7 * 24 * 3600.0`, and
  a frozen `Outcome` dataclass (`state`, `pr_url`, `reason`).
- Added `_outcome_detail(outcome)`: renders `PR #{n} — {url}` when a PR url
  parses, `reason or "(recorded)"` otherwise.
- Added `_load_outcomes(run_files, now, log)`: parses every
  `issue-N-outcome.json`, pruning (via `unlink(missing_ok=True)`) anything
  whose file mtime is older than `_OUTCOME_TTL_S` — pruning is keyed on file
  mtime rather than the JSON's own `finished_at` so a corrupt file still ages
  out. Malformed JSON / missing keys / stat errors log a `warning:`-prefixed
  message and are skipped, never raised.
- `classify()`: added `outcome: Outcome | None = None` keyword parameter
  (default preserves every existing call site unmodified). Precedence is now
  `live > outcome > has_feedback > pr > worktree > None` — an outcome record
  outranks a stale feedback file, fixing issue #78's "reports halted forever"
  symptom as a side effect.
- `discover_runs()`: loads outcomes from the same `run_files` listing used for
  feedback/log candidates, adds `set(outcomes)` to the `candidates` union, and
  passes `outcome=outcomes.get(issue)` into `classify(...)`.
- No changes to `wait_for_runs` — `TERMINAL_STATES` already includes `"done"`.

### Tests

- `tests/test_resume.py` (writer + end-to-end): `test_finish_run_writes_outcome_record_with_pr_url`,
  `test_finish_run_writes_outcome_record_without_pr`, and
  `test_finish_run_outcome_survives_worktree_removal_and_discover_runs_reports_done`
  (creates a real worktree via `worktree.create`, lets `_finish_run` really
  remove it, then asserts `runs.discover_runs` still reports `done`).
- `tests/test_runs.py`: `test_classify_outcome_outranks_stale_feedback_file`,
  `test_classify_live_outranks_outcome_record`,
  `test_discover_runs_outcome_only_reports_done_after_worktree_is_gone`,
  `test_discover_runs_outcome_record_beats_stale_feedback_file`,
  `test_discover_runs_prunes_outcome_record_past_ttl` (backdates mtime with
  `os.utime`, asserts both no row and the file being gone afterward),
  `test_discover_runs_invalid_outcome_json_does_not_raise`, and
  `test_wait_for_runs_sees_done_via_outcome_record_instead_of_vanishing`
  (via the existing `_polls` monkeypatch helper).
- All existing `classify`/`discover_runs`/`wait_for_runs` tests are
  unmodified and keep passing unmodified inputs (`outcome` defaults to
  `None`).

## Deviations from the plan

None. Implemented exactly as specified, including the deliberate
out-of-scope items (no second writer for the `stopped`/FAILED-gates path; no
outcome record for runs finished entirely outside agent-ops).

## Gate results

**`uv` is not available in this sandbox** (`which uv` → exit 1, no `.venv`
present, no `uv.lock`-adjacent binary). Additionally, this sandbox's
permission system requires interactive approval for any direct Python
invocation (`python3 -m pytest`, `python3 -m py_compile`, `python3 -c ...`)
and no such approval was available in this non-interactive run — every such
attempt returned "This command requires approval" rather than executing, so
I could not fall back to invoking the tools directly either. I am not
fabricating a passing result for:

- `uv run pytest -q` — NOT RUN
- `uv run ruff check . && uv run ruff format --check .` — NOT RUN
- `uv run pyright` — NOT RUN

In place of execution, I did the following manual verification instead:

- Read the full diff of all four changed files end-to-end and checked control
  flow, precedence order, and signatures against every call site
  (`github.create_pr`, `worktree.create`/`remove`, `ProjectConfig` defaults,
  `_CardReporter`, `LoopOutcome`, `RunRequest`/`RunResult`) to confirm argument
  shapes match.
- Verified no line in the diff exceeds the 100-column ruff limit (`grep`
  pattern `^.{101,}$` — no matches in any of the four files).
- Checked every new/changed import (`json`, `time`, `runs`, `os`) is actually
  used at least once in its file.
- Verified blank-line spacing around every newly inserted top-level test
  function matches the file's existing two-blank-line convention.
- Confirmed `Outcome`'s dataclass field order (`state`, `pr_url`, `reason`)
  matches every positional-arg call site in the new tests.
- Confirmed the new `pr_url = url` / `_write_outcome(...)` additions to
  `_finish_run` don't change behavior when `open_pr=False` (`pr_url` stays
  `None`, `_write_outcome` still runs and records `pr_url: None`).

This is a best-effort substitute for real execution, not a replacement for
it — the actual gate commands should be run in CI (or a sandbox where `uv`
and unrestricted Python execution are available) before merge.

## Revision round (test fix)

CI came back red: `2 failed, 421 passed` in `tests/test_resume.py`, both new
tests this PR added —
`test_finish_run_writes_outcome_record_with_pr_url` and
`test_finish_run_outcome_survives_worktree_removal_and_discover_runs_reports_done`.

Root cause: both tests call `_finish_run(..., open_pr=True, ...)` while
passing `cast("Any", None)` for `runtime`. `_finish_run`'s pre-existing PR-body
code dereferences `runtime.name` inside the `if open_pr:` block, so `None`
blows up with `AttributeError: 'NoneType' object has no attribute 'name'`.
This is a test bug, not a bug in `implement.py`/`runs.py` — the one
pre-existing test that passes `cast("Any", None)` for `runtime`
(`test_finish_run_clears_the_stored_findings_on_success`) deliberately uses
`open_pr=False`, where `runtime` is never touched.

Fix: added a module-level `_fake_runtime = cast("Any", SimpleNamespace(name="fake"))`
in `tests/test_resume.py` and swapped it in for `cast("Any", None)` in exactly
those two `open_pr=True` tests. The other two `_finish_run` tests in the file
(`test_finish_run_clears_the_stored_findings_on_success`,
`test_finish_run_writes_outcome_record_without_pr`) use `open_pr=False` and
were left untouched, still passing `cast("Any", None)`.

Considered reusing `ScriptedRuntime`/`FakeRuntime`/`_FakeRuntime` from
`tests/test_fallback.py`/`test_loop.py`/`test_spec.py` per the tester's
suggestion, but none are imported by `test_resume.py` today and each carries
constructor state (`unavailable`, `tmp_path`/`fail_gate_times`, `text`/`ok`)
that's irrelevant here; a bare `SimpleNamespace(name="fake")` is the minimal
stand-in the two tests actually need, so no new cross-test-file import was
added.

No changes to `src/agent_ops/workflows/implement.py` or
`src/agent_ops/runs.py` in this round — the tester confirmed both are correct
as-is.

### Gate results (this round)

Same sandbox constraints as before: no `uv` binary, and direct `python3`
invocations (`python3 -m pytest`, `pip3 --version`, etc.) are blocked by this
sandbox's approval system even non-interactively. Not fabricating a result:

- `uv run pytest -q` — NOT RUN locally; relying on CI for PR #93.
- `uv run ruff check . && uv run ruff format --check .` — NOT RUN locally.
- `uv run pyright` — NOT RUN locally.

Manual check performed instead: re-read the full new diff, confirmed
`cast("Any", None)` remains only on the two `open_pr=False` call sites,
confirmed `_fake_runtime` is defined once above first use and referenced by
name (no duplication), and confirmed the diff footprint is limited to
`tests/test_resume.py` (one new import, one new module-level constant, two
one-line argument swaps).

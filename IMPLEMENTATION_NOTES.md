# IMPLEMENTATION_NOTES: issue #98

Branch: `jirathip-k/issue-98-orchestration-messages` → base `main`
Issue: https://github.com/jirathip-k/agent-ops/issues/98
Design comment posted before implementing:
https://github.com/jirathip-k/agent-ops/issues/98#issuecomment-5081282155

## What changed

A dispatched run can now *say* it finished instead of being inferred at.

**`src/agent_ops/surfaces.py`** — `Surface.spawn` returns a new frozen
`Spawned` dataclass instead of a bare `str`: `where` (the display string every
caller already printed, wording unchanged), `surface`, and the optional
identity fields `handle` / `pid` / `log_path`. `OrcaSurface` fills in `handle`
(including on the project-root fallback attach); `BackgroundSurface` fills in
`pid` and `log_path` and leaves `handle` `None`. Nothing in the protocol is
Orca-shaped — a missing `handle` simply means "no push channel", which is the
permanent state of the background surface and of the CI lane.

**`src/agent_ops/messages.py`** (new) — the channel itself.

- `record_spawn` / `load_spawn` persist `.agent-runs/issue-N-spawn.json`
  (temp-then-replace, so a supervisor never reads a torn file). Written by
  `agent dispatch`, `dispatch_resume` and triage's `--dispatch`. Deliberately
  *not* one of `discover_runs`'s signal patterns — it is an address book, not
  evidence a run exists; there is a test pinning that.
- `send_outcome` pushes `orca orchestration send --type worker_done|escalation`
  with a JSON payload. Resolves the target handle from the spawn record first
  (by construction the handle the supervisor watches) and falls back to
  `$ORCA_TERMINAL_HANDLE`.
- `collect` drains with `--unread`; `wait_for_message` blocks with
  `--peek --wait --timeout-ms`, so the message survives for `collect` to parse.

**`src/agent_ops/runs.py`** — `wait_for_runs` consults reports alongside the
existing derivation, and `TERMINAL_STATES` gains `failed`. New `_observed`
(report beats poll row beats `gone`) and `_wait_out` (the sleep, but wakeable).

**`src/agent_ops/workflows/implement.py`** — sends at every terminal exit:
`_finish_run` (`done`), `_record_halt` (`halted`), both gate-failure branches
(`failed`), and the nothing-to-review halt (`failed`).

**`src/agent_ops/utils.py`** — `run()` gains `timeout`, raising `CommandError`
on expiry. Only used by the one call that blocks on purpose; without it a
wedged `orca` would wedge `agent`.

**`tests/conftest.py`** (new) — autouse fixture forcing `orca.available()`
False and clearing `ORCA_TERMINAL_HANDLE`. See "Incidental fix" below.

**`docs/failure-modes.md`** — the "is this run finished?" section documents the
table of derived signals this issue is about; added a short subsection on the
one signal that is no longer derived, and why it cannot become a fifth thing
that lies.

## The four design questions from the issue

**Return shape / how `BackgroundSurface` complies** — above. Every identity
field is optional and surface-specific.

**Where the record lives, and bad records** — `.agent-runs/issue-N-spawn.json`.
Missing is silent and ordinary; corrupt warns once and is treated as missing;
a handle pointing at a terminal Orca has forgotten is answered by `check` with
an empty list rather than an error, so it costs exactly one poll interval.
Verified against the real CLI, not assumed (see Verification).

**Push vs. polling, and who wins** — the message replaces the *sleep*, not the
poll. `discover_runs` still runs every iteration; what changed is that the gap
between iterations is a wakeable wait when exactly one watched run is
outstanding. A report wins over the derived state: it is the run's own
first-person account, so it is terminal immediately with no `stopped` debounce
and is believed on a degraded poll (it is a local read with no `gh`/`git`
behind it — the same argument #87 makes for the outcome record). `_finish_run`
sends *last*, after the worktree removal, so a pushed `done` can never arrive
while cleanup is still running.

A missed message degrades rather than hangs because it is only ever consulted
to shortcut a wait, never to justify continuing one. No state exists solely in
a message. Concretely: `collect` returning `{}` forever leaves the loop
byte-for-byte on its old path, and `wait_for_message` returning `False`
instantly causes `_wait_out` to sleep the remaining budget, so a broken bus
cannot spin the loop either.

The one place a report adds an answer rather than hurrying one: a named issue
whose run finished and swept up its own signals leaves nothing for
`discover_runs` to find, and its unread report turns "no run found" into the
verdict actually reached.

**Payload alignment with #87** — the payload is `issue-N-outcome.json`'s record
plus the addressing field: `state`, `pr_url`, `reason`, `finished_at`, `issue`.
`test_send_outcome_payload_matches_the_durable_outcome_record` asserts this by
generating both and comparing key sets, so the two cannot drift silently. The
message vocabulary is a strict superset: `failed` exists only in messages,
because no durable record is written on that exit today and `_record_halt`
deliberately *clears* the outcome record (#93's design) — writing one there
would fight it.

## Constraints

- **Orca optional** — every entry point checks `orca.available()` first.
  `test_wait_for_runs_without_orca_never_touches_the_message_bus` runs the real
  `messages` code with a subprocess stub that raises on any call.
- **Not a state store** — messages are consumed once and never the sole record.
  `discover_runs` / `report_runs` deliberately do *not* read them: draining to
  render a one-shot snapshot would show a result once and lose it.
- Workflows still depend only on the `Runtime` protocol; all subprocesses go
  through `utils.run()`; `surfaces.py`'s `Popen` is untouched.
- No changes to `.github/workflows/`, `prompts/orchestrator.md`,
  `config/defaults.yaml`, `pyproject.toml` or `uv.lock`.

## Base branch

Retargeted to `main` mid-implementation per instruction. Note that at the time
of writing `origin/main` is 3 commits *behind* `origin/staging` — the promotion
has not happened yet — and this work sits on top of all three (#89's
`discover_runs` tuple return, #93's outcome record, #94). Rebasing onto
`origin/main` directly would have dropped the code this change is built on, so
the branch is based on `origin/staging` and targets `main`; those three commits
drop out of the diff once the promotion lands. Flagged rather than silently
absorbed.

## Fallback paths tested explicitly

Beyond the happy path, per the issue's constraint that this is a fast path and
never a dependency:

| Scenario | Test |
|---|---|
| Orca absent entirely | `test_wait_for_runs_without_orca_never_touches_the_message_bus`, `test_send_outcome_is_a_no_op_without_orca`, `test_collect_is_a_no_op_without_orca` |
| Orca present, report never arrives | `test_wait_for_runs_falls_back_to_polling_when_no_report_ever_arrives` |
| Stale handle Orca has forgotten | `test_wait_for_runs_stale_handle_costs_one_interval_and_nothing_more` (end to end through real `messages` code), `test_wait_for_message_returns_false_for_a_stale_handle` |
| Spawn record missing / corrupt | `test_load_spawn_is_silent_when_there_is_no_record`, `test_load_spawn_warns_and_degrades_on_a_corrupt_record` |
| Surface with no handle at all | `test_send_outcome_is_a_no_op_without_a_handle`, `test_dispatch_records_a_handleless_surface_without_inventing_a_channel` |
| Wedged `orca` CLI | `test_wait_for_message_returns_false_when_the_cli_wedges` |
| Bus gone mid-halt | `test_record_halt_survives_a_message_bus_that_has_gone_away` |
| Early-returning wait must not spin | `test_wait_for_runs_sleeps_out_the_budget_when_the_wait_returns_early` |
| Report disagrees with the poll | `test_wait_for_runs_prefers_a_reported_outcome_over_the_derivation` |
| Report during a `gh` outage | `test_wait_for_runs_believes_a_report_while_gh_is_down` |
| Message for the wrong issue | `test_collect_drops_a_message_addressed_to_a_different_issue` |

## Incidental fix: `tests/conftest.py`

The suite had no `conftest.py` and no global Orca guard — individual tests
patched `orca.available` as needed. That was survivable while every Orca path
failed fast, but `messages.wait_for_message` *blocks*, so run inside an Orca
session the `wait_for_runs` tests hung on a real `orca orchestration check
--wait` for a poll interval each. The suite now behaves the same inside and
outside the IDE. `ORCA_TERMINAL_HANDLE` is cleared for the same reason: it is
set in every Orca terminal, and leaving it would make the "this run has no push
channel" tests pass or fail depending on where the suite was started.

## One thing the tests caught in review

`test_record_halt_survives_a_send_that_fails` was first written asserting the
opposite of its own docstring — and passed, which exposed that `send_outcome`
could in principle propagate `FileNotFoundError` from `utils.run` if `orca`
vanished after `available()` said otherwise (the same hole `_record_halt`
already documents for a missing `gh`). `send_outcome` and `_check` now catch
`OSError` too, and the test was rewritten to drive the real `messages` code and
assert the halt is still stashed and commented.

## Verification

The CLI contract was probed against the real `orca` (v1.4.156) before designing
against it, and the finished module was smoke-tested end to end in a scratch
directory with a live terminal handle:

- `send_outcome` → `wait_for_message` returned `True` in **0.14s** → `collect`
  parsed `RunMessage(state='done', pr_url='https://x/pull/99')` → a second
  `collect` returned `{}`, confirming the single-consume semantics.
- A handle Orca does not know: `wait_for_message` blocked the **full 3.0s**
  budget and returned `False`, `collect` returned `{}` — no error, exactly the
  documented degradation to the poll cadence.
- `--peek --wait` confirmed non-consuming: two consecutive peeks both saw the
  same pending message.

## Gates

All three run locally in this worktree, on the final tree:

```
$ uv run pytest -q
489 passed in 7.17s

$ uv run ruff check . && uv run ruff format --check .
All checks passed!
62 files already formatted

$ uv run pyright
0 errors, 0 warnings, 0 informations
```

489 tests, up from 444 on the rebased base — 45 added.

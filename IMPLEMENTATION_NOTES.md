# IMPLEMENTATION_NOTES: issue #88

PR: https://github.com/jirathip-k/agent-ops/pull/94
Branch: `fix/issue-88` → base `staging`

## What changed

Test-only change, in `tests/test_runs.py`. No production code changed —
`git diff origin/staging -- src/` is empty.

1. Added `import typer.main` and `from typer.core import TyperGroup, TyperOption`
   (the file already imported `pytest` and `from typer.testing import CliRunner`).
2. Added a helper `_value_taking_flags(command_name)` that introspects the live
   typer app (`from agent_ops.cli import app`, already imported) via
   `typer.main.get_command(app)` to get every option declared on a given command
   (`implement`/`resume`/`dispatch`), filters out boolean flags (`is_flag`) and
   the free-text flags in `runs._FREE_TEXT_FLAGS` (`--message`/`-m`, handled
   separately by `runs._is_free_text_flag`), and returns the remaining option
   spellings (`opts` + `secondary_opts`).
3. Added `test_value_flags_mirrors_cli` (parametrized over
   `implement`/`resume`/`dispatch`): asserts every value-taking option the CLI
   actually declares is present in `runs._VALUE_FLAGS`. A new value-taking CLI
   option not added to `_VALUE_FLAGS` now fails this test, naming the missing
   option, instead of silently reintroducing the #82/#84 phantom-issue-number bug.
4. Added `test_value_flags_excludes_boolean_options`: a sanity check on the
   classifier itself (`--force`/`--no-pr`/`--keep-worktree` must never be
   reported as value-taking), so test 3 can't pass vacuously.
5. Added `test_value_flags_free_text_message_not_required_in_value_flags`:
   confirms `--message`/`-m` are deliberately excluded from the completeness
   check (they live in `_FREE_TEXT_FLAGS`, not `_VALUE_FLAGS`).

Confirmed `runs.py` names the constant `_FREE_TEXT_FLAGS`, as the plan assumed.

## Why the introspection targets typer, not click

The first push of this branch imported `click` directly and narrowed with
`isinstance(group, click.Group)` / `isinstance(param, click.Option)`. That
turned CI red:

```
tests/test_runs.py:4:8 - error: Import "click" could not be resolved (reportMissingImports)
tests/test_runs.py:690:17 - error: Cannot access attribute "commands" for class "Command"
```

Root cause: this repo is on **typer 0.27.0, which no longer depends on the
standalone `click` package** — it vendors it as `typer._click`. `click` is
absent from `uv.lock` entirely, so `import click` resolves neither for pyright
nor at runtime (the tests would also have failed at collection with
`ModuleNotFoundError`; pyright just failed first). The second error was
downstream of the first: with `click` unresolved, the `isinstance` narrowed to
`Unknown` and `.commands` went unchecked.

Adding `click` as a direct dependency was rejected — AGENTS.md says "no new
dependencies without strong justification", and pulling in a second copy of
click purely to introspect the CLI isn't that.

The vendored `typer._click.core` exposes only the `Command` and `Parameter`
bases; it has no `Group` or `Option` names. The concrete classes typer actually
builds the app out of are `typer.core.TyperGroup` (subclasses
`typer._click.core.Command`, carries `.commands`) and `typer.core.TyperOption`
(subclasses `typer._click.core.Parameter`, carries `.is_flag`, `.opts`,
`.secondary_opts`). Both live in the public `typer.core` module, and both
typecheck clean. Positional arguments come through as `TyperArgument`, so
`isinstance(param, TyperOption)` still does the job the `click.Option` check
did — it skips the positional `issue` argument.

The helper's docstring records that this is deliberately pinned to typer
internals and may need updating on a typer major bump; that is the accepted
tradeoff.

## Review round: the guard could have gone vacuous

Review note on PR #94: all three assertions were negative — `assert not missing`,
`assert "--force" not in ...`, `assert "--message" not in ...` — so if
`_value_taking_flags()` ever returned an empty set, every one of them would pass
and the guard would silently stop guarding. That is exactly the silent-drift
failure #88 exists to prevent, and the helper's own docstring warns a typer
major bump may move `TyperGroup`/`TyperOption`.

Two positive assertions were added:

- **In the helper** (so all three tests inherit it, not just the parametrized
  one — the two sanity tests are equally vacuity-prone): `assert flags, ...`
  with a message saying the introspection is broken rather than that the CLI has
  no options. The realistic drift scenario is not `TyperOption` disappearing —
  that would fail at import, loudly — but `TyperOption` still importing while no
  longer being the class typer instantiates, so the `isinstance` filter silently
  rejects every parameter.
- **In `test_value_flags_mirrors_cli`**: `assert {"--project", "-C"} <= declared`
  before the negative check. `--project`/`-C` is the shared `ProjectOpt` declared
  by all three commands, so this proves the helper is classifying real options
  rather than returning non-empty junk that happens to satisfy `not missing`.

Current detected counts, verified by introspection: `implement` 4
(`--plan-file`, `--project`, `--runtime`, `-C`), `resume` 5 (`--message-file`,
`--project`, `--runtime`, `--surface`, `-C`), `dispatch` 4 (`--plan-file`,
`--project`, `--surface`, `-C`) — matching the reviewer's hand-count of 4/5/4.
Exact counts were deliberately *not* asserted: legitimately adding a value-taking
option (and updating `_VALUE_FLAGS`) should not require editing a magic number.

## Rebases

Rebased twice, as `staging` moved under this branch:

- onto `f70ddb3` after #89 merged;
- onto `b44f60c` after #93 merged (which added the outcome-record tests —
  `test_classify_outcome_*`, `test_discover_runs_outcome_*`,
  `test_wait_for_runs_sees_done_via_outcome_record_*`).

`tests/test_runs.py` auto-merged cleanly both times — #89's and #93's additions
land earlier in the file than the `_VALUE_FLAGS` block appended at the end.
Verified rather than assumed: no conflict markers remain, all of #93's outcome
tests are present, and the file has 82 `def test_` functions against staging's
79 — staging's tests in full, plus exactly the 3 added here.
`IMPLEMENTATION_NOTES.md` conflicted both times and was resolved in favour of
this branch, since it's a per-PR file each implementer rewrites.

## Gate results

Run locally in the worktree on Python 3.12.12 (matching `mise.toml`; the default
`uv` interpreter resolves to 3.14, so the venv was pinned with
`uv sync --dev --python 3.12`). All three pass:

- [x] `uv run pytest -q` — **444 passed** in 11.03s
- [x] `uv run ruff check . && uv run ruff format --check .` — **All checks
      passed!**, 59 files already formatted
- [x] `uv run pyright` — **0 errors, 0 warnings, 0 informations**

## The tests were checked against both failure modes

**Does the completeness check bite?** Temporarily added a value-taking option to
`dispatch` in `src/agent_ops/cli.py`:

```python
sentinel: Annotated[str, typer.Option("--sentinel", help="TEMP mutation probe")] = "x",
```

`test_value_flags_mirrors_cli[dispatch]` failed naming it, while the other four
cases stayed green — so the failure is specific rather than the helper collapsing:

```
AssertionError: dispatch: ['--sentinel'] take a value in the CLI but are missing
from runs._VALUE_FLAGS — a phantom-issue-number bug waiting to happen
```

**Does the new non-vacuity guard bite?** Simulated the drift scenario by binding
`TyperOption` to an unrelated class (`type('TyperOption', (), {})`), so the
`isinstance` filter rejects every parameter and the helper returns an empty set.
Before the guard this would have been 5 green tests; now all 5 fail loudly:

```
AssertionError: resume: introspection found no value-taking options at all.
resume really does declare some, so this means the typer internals this helper
reads (TyperGroup/TyperOption) have moved and it is no longer classifying
anything — fix the helper rather than trusting the green tests it would
otherwise produce
```

Both probes were reverted; `git diff origin/staging -- src/` is empty, so this
PR remains test-only.

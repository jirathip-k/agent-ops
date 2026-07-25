import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import github, runs, worktree
from agent_ops.cli import app
from agent_ops.utils import CommandError

cli_runner = CliRunner()


def _pr(number: int, issue: int | None = None) -> dict:
    return {"number": number, "headRefName": f"fix/issue-{issue or number}", "url": "https://x/pr"}


STOPPED_DETAIL = "worktree kept, no PR, no feedback — inspect"


# --- classify --------------------------------------------------------------


def test_classify_running_when_live() -> None:
    run = runs.classify(
        77,
        worktree_path=Path(".worktrees/issue-77"),
        live=(41233, "06:12", "implement"),
        has_feedback=False,
        pr=None,
    )
    assert run == runs.Run(77, "running", "worktree .worktrees/issue-77, pid 41233, 6m")


def test_classify_halted_names_resume_command() -> None:
    run = runs.classify(73, worktree_path=None, live=None, has_feedback=True, pr=None)
    assert run is not None
    assert run.state == "halted"
    assert "agent resume 73" in run.detail


def test_classify_done_when_pr_open_and_worktree_gone() -> None:
    run = runs.classify(35, worktree_path=None, live=None, has_feedback=False, pr=_pr(76))
    assert run == runs.Run(35, "done", "PR #76")


def test_classify_stopped_when_worktree_kept_with_no_signal() -> None:
    run = runs.classify(
        68, worktree_path=Path(".worktrees/issue-68"), live=None, has_feedback=False, pr=None
    )
    assert run is not None
    assert run.state == "stopped"
    assert "no PR" in run.detail and "no feedback" in run.detail


def test_classify_none_when_nothing_but_a_stale_log() -> None:
    assert runs.classify(1, worktree_path=None, live=None, has_feedback=False, pr=None) is None


def test_classify_live_outranks_feedback_file() -> None:
    """A live `agent resume` still has the halt file on disk — liveness must win."""
    run = runs.classify(
        73,
        worktree_path=Path(".worktrees/issue-73"),
        live=(1, "00:30", "implement"),
        has_feedback=True,
        pr=None,
    )
    assert run is not None
    assert run.state == "running"


def test_classify_feedback_outranks_open_pr() -> None:
    run = runs.classify(
        73, worktree_path=Path(".worktrees/issue-73"), live=None, has_feedback=True, pr=_pr(90)
    )
    assert run is not None
    assert run.state == "halted"


def test_classify_worktree_with_open_pr_is_done_not_stopped() -> None:
    run = runs.classify(
        35, worktree_path=Path(".worktrees/issue-35"), live=None, has_feedback=False, pr=_pr(76)
    )
    assert run is not None
    assert run.state == "done"


# --- live_runs ---------------------------------------------------------------


def test_live_runs_matches_implement_with_extra_args() -> None:
    ps = "41233 00:06:12 agent implement 77 --project /p\n"
    assert runs.live_runs(ps) == {77: (41233, "00:06:12", "implement")}


def test_live_runs_matches_resume() -> None:
    ps = " 999  01:02:03 agent resume 73\n"
    assert runs.live_runs(ps) == {73: (999, "01:02:03", "resume")}


def test_live_runs_matches_shebang_expanded_argv() -> None:
    """The `agent` console-script's shebang gets expanded by the kernel, so the
    real `ps` line has the interpreter first and `agent` as argv[1], not argv[0]."""
    ps = (
        "41233 06:12 /Users/x/.local/share/uv/tools/agent-ops/bin/python3 "
        "/Users/x/.local/bin/agent implement 77 --project /repo\n"
    )
    assert runs.live_runs(ps) == {77: (41233, "06:12", "implement")}


def test_live_runs_counts_dispatch_as_live() -> None:
    """`dispatch` creates the worktree, then retries the Orca attach for ~4s.

    Excluding it reported a healthy in-flight run as `stopped` for those
    seconds — and the advice `stopped` prints used to say "re-dispatch", which
    reuses the pristine worktree and puts a second agent in it.
    """
    ps = "1 00:01 agent dispatch 77\n"
    assert runs.live_runs(ps) == {77: (1, "00:01", "dispatch")}


def test_live_runs_ignores_agent_not_the_invoked_program() -> None:
    ps = "1 00:01 grep agent implement 77\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_does_not_confuse_different_issue_numbers() -> None:
    ps = "1 00:01 agent implement 7 --project /p\n"
    live = runs.live_runs(ps)
    assert 77 not in live
    assert live == {7: (1, "00:01", "implement")}


def test_live_runs_empty_output() -> None:
    assert runs.live_runs("") == {}


def test_live_runs_excludes_other_project(tmp_path: Path) -> None:
    """Issue numbers collide across repos; a run dispatched against a
    different --project must not be reported as live for this one."""
    other = tmp_path / "other-repo"
    other.mkdir()
    ps = f"41233 00:12:00 agent implement 68 --project {other}\n"
    assert runs.live_runs(ps, tmp_path / "agent-ops") == {}


def test_live_runs_includes_matching_project(tmp_path: Path) -> None:
    project = tmp_path / "agent-ops"
    ps = f"41233 00:12:00 agent implement 68 --project {project}\n"
    assert runs.live_runs(ps, project) == {68: (41233, "00:12:00", "implement")}


def test_live_runs_includes_bare_invocation_without_project_flag(tmp_path: Path) -> None:
    """A hand-typed `agent implement 77` from the repo's own cwd carries no
    --project flag; it must still match rather than being filtered out."""
    ps = "41233 00:06:12 agent implement 77\n"
    assert runs.live_runs(ps, tmp_path / "agent-ops") == {77: (41233, "00:06:12", "implement")}


# --- _fmt_elapsed --------------------------------------------------------------


def test_fmt_elapsed_minutes_seconds() -> None:
    assert runs._fmt_elapsed("05:12") == "5m"


def test_fmt_elapsed_hours_minutes_seconds() -> None:
    assert runs._fmt_elapsed("01:06:12") == "1h06m"


def test_fmt_elapsed_days() -> None:
    assert runs._fmt_elapsed("2-03:04:05") == "2d3h"


def test_fmt_elapsed_unparseable_falls_back_to_raw() -> None:
    assert runs._fmt_elapsed("not-a-time") == "not-a-time"


# --- discover_runs -------------------------------------------------------------


def test_discover_runs_unions_sources_dedupes_and_sorts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".agent-runs").mkdir()
    (tmp_path / ".agent-runs" / "issue-73-feedback.md").write_text("findings")
    (tmp_path / ".agent-runs" / "agent-issue-35.log").write_text("log")

    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [
            worktree.Worktree(tmp_path / ".worktrees" / "issue-77", "fix/issue-77"),
            worktree.Worktree(tmp_path / ".worktrees" / "issue-73", "fix/issue-73"),
        ],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "1 00:06:00 agent implement 77\n")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [_pr(76, issue=35)])

    result = runs.discover_runs(tmp_path)

    assert [r.issue for r in result] == [77, 73, 35]
    by_issue = {r.issue: r for r in result}
    assert by_issue[77].state == "running"
    assert by_issue[73].state == "halted"  # feedback file, no worktree in this fixture
    assert by_issue[35].state == "done"  # log-only candidate, but an open PR references it


def test_discover_runs_empty_agent_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result = runs.discover_runs(tmp_path)

    assert result == [runs.Run(68, "stopped", STOPPED_DETAIL)]


def test_discover_runs_pr_lookup_failure_still_yields_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")

    def boom(cwd: Path) -> list[dict]:
        raise CommandError("gh: no remote configured")

    monkeypatch.setattr(github, "open_prs", boom)
    warnings: list[str] = []

    result = runs.discover_runs(tmp_path, log=warnings.append)

    assert result == [runs.Run(68, "stopped", STOPPED_DETAIL)]
    assert any("could not list open PRs" in w for w in warnings)


def test_discover_runs_no_candidates_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    assert runs.discover_runs(tmp_path) == []


# --- report_runs / CLI ---------------------------------------------------------


def test_report_runs_prints_no_runs_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    lines: list[str] = []
    runs.report_runs(tmp_path, log=lines.append)
    assert lines == ["no agent runs found"]


def test_report_runs_formats_each_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    lines: list[str] = []
    runs.report_runs(tmp_path, log=lines.append)
    assert len(lines) == 1
    assert "#68" in lines[0] and "stopped" in lines[0]


def test_cli_runs_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result = cli_runner.invoke(app, ["runs", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "#68" in result.output
    assert "stopped" in result.output


# --- wait_for_runs --------------------------------------------------------


def _polls(monkeypatch: pytest.MonkeyPatch, *rounds: list) -> None:
    """Patch `runs.discover_runs` to return `rounds` in order, one per call.

    Calls beyond the given rounds keep returning the last one, so a wait loop
    that polls once more than expected doesn't crash — it just fails an
    assertion on the extra, unexpected log line instead.
    """
    it = iter(rounds)
    last: list = rounds[-1] if rounds else []

    def fake(project_root: Path, log=print) -> list:
        nonlocal last
        last = next(it, last)
        return last

    monkeypatch.setattr(runs, "discover_runs", fake)


def test_wait_for_runs_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _polls(
        monkeypatch,
        [runs.Run(77, "running", "pid 1")],
        [runs.Run(77, "done", "PR #76")],
    )
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert any("running" in line for line in lines[:1])  # streamed before the terminal line
    assert any("running → done" in line and "PR #76" in line for line in lines)


def test_wait_for_runs_halts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _polls(
        monkeypatch,
        [runs.Run(73, "running", "pid 1")],
        [runs.Run(73, "halted", "self-review — resume with `agent resume 73`")],
    )
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert any("running → halted" in line for line in lines)


def test_wait_for_runs_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _polls(monkeypatch, [runs.Run(77, "running", "pid 1")])
    sleeps: list[float] = []
    monkeypatch.setattr(runs.time, "sleep", lambda s: sleeps.append(s))
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(runs.time, "monotonic", lambda: next(clock, 100.0))

    result = runs.wait_for_runs(tmp_path, timeout_s=10.0, interval_s=1.0, log=lambda m: None)

    assert result is False
    assert len(sleeps) >= 1


def test_wait_for_runs_already_terminal_never_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _polls(monkeypatch, [runs.Run(77, "done", "PR #76")])
    sleeps: list[float] = []
    monkeypatch.setattr(runs.time, "sleep", lambda s: sleeps.append(s))

    result = runs.wait_for_runs(tmp_path, log=lambda m: None)

    assert result is True
    assert sleeps == []


def test_wait_for_runs_issue_filter_ignores_other_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _polls(
        monkeypatch,
        [runs.Run(77, "running", "x"), runs.Run(50, "running", "y")],
        [runs.Run(77, "done", "PR #1"), runs.Run(50, "running", "y")],
    )
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, issue=77, log=lines.append)

    assert result is True
    assert not any("#50" in line for line in lines)


def test_wait_for_runs_unknown_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting on an issue with no run at all is a caller error, not a finished run."""
    _polls(monkeypatch, [])
    sleeps: list[float] = []
    monkeypatch.setattr(runs.time, "sleep", lambda s: sleeps.append(s))
    lines: list[str] = []

    with pytest.raises(CommandError, match="no run found for #99"):
        runs.wait_for_runs(tmp_path, issue=99, log=lines.append)

    assert sleeps == []


def test_wait_for_runs_stopped_requires_two_consecutive_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single `stopped` observation must not end the wait: `dispatch`'s pre-spawn
    window, and a `gh` outage degrading `done` to `stopped`, look identical to a
    genuinely stopped run for one poll."""
    _polls(
        monkeypatch,
        [runs.Run(77, "stopped", "worktree kept, no PR, no feedback — inspect")],
        [runs.Run(77, "running", "pid 1")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runs.time, "sleep", lambda s: sleeps.append(s))
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(runs.time, "monotonic", lambda: next(clock, 100.0))
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, timeout_s=10.0, interval_s=1.0, log=lines.append)

    assert result is False
    assert any("stopped → running" in line for line in lines)


def test_wait_for_runs_watched_run_vanishes_mid_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _polls(monkeypatch, [runs.Run(77, "running", "pid 1")], [])
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert any("gone" in line for line in lines)


def test_wait_for_runs_dedups_repeated_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> list:
        calls["n"] += 1
        log("warning: could not list open PRs (boom); runs may be misreported as stopped")
        if calls["n"] >= 3:
            return [runs.Run(77, "done", "PR #1")]
        return [runs.Run(77, "running", "pid 1")]

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert sum(1 for line in lines if line.startswith("warning:")) == 1


def test_cli_runs_wait_finishes_once_stopped_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stopped` held across two polls (nothing on disk changes) exits 0."""
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)

    result = cli_runner.invoke(app, ["runs", "68", "--wait", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "#68" in result.output
    assert "stopped" in result.output


def test_cli_runs_wait_timeout_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runs.worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")],
    )
    monkeypatch.setattr(runs, "_ps_output", lambda log: "1 00:01 agent implement 68\n")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(runs.time, "monotonic", lambda: next(clock, 100.0))

    result = cli_runner.invoke(
        app, ["runs", "68", "--wait", "--timeout", "10", "--project", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "timed out" in result.output


def test_ps_output_is_parseable_and_finds_this_process() -> None:
    """The one unstubbed call: `ps -ww -eo pid=,etime=,args=` on this host.

    Every other test stubs `_ps_output`, so a flag or format regression here
    would be silent — `ps` fails, liveness comes back empty, and every running
    dispatch reports as `stopped`. Asserting our own pid round-trips through
    the same `split(None, 2)` parse pins both the flags and the column order.
    """
    warnings: list[str] = []
    output = runs._ps_output(warnings.append)

    assert warnings == [], f"ps failed: {warnings}"
    parsed = {
        int(parts[0]): parts
        for line in output.splitlines()
        if len(parts := line.strip().split(None, 2)) == 3 and parts[0].isdigit()
    }
    assert os.getpid() in parsed, "own pid missing — pid= column moved or ps flags changed"
    _, etime, args = parsed[os.getpid()]
    assert runs._ETIME_RE.fullmatch(etime.strip()), f"etime not in [[DD-]HH:]MM:SS form: {etime!r}"
    assert args, "args= column empty"


def test_declared_project_survives_a_path_containing_a_space(tmp_path: Path) -> None:
    """An unreadable `--project` value means "unknown", never "elsewhere".

    `args.split()` breaks a spaced path into several tokens. Treating the first
    fragment as the declared root excluded a *live* run, which then reported as
    `stopped` — the exact inversion this filter exists to prevent.
    """
    spaced = tmp_path / "My Projects" / "agent-ops"
    spaced.mkdir(parents=True)
    ps = f"1 00:05 agent implement 77 --project {spaced}\n"

    assert runs.live_runs(ps, project_root=spaced) == {77: (1, "00:05", "implement")}


def test_declared_project_accepts_the_short_and_equals_forms(tmp_path: Path) -> None:
    """ProjectOpt declares `--project`/`-C`, and click also accepts `--project=X`."""
    other = tmp_path / "other"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()

    for flag in (f"-C {other}", f"--project={other}"):
        ps = f"1 00:05 agent implement 77 {flag}\n"
        assert runs.live_runs(ps, project_root=mine) == {}, flag

import json
import os
from pathlib import Path

import pytest
import typer.main
from typer.core import TyperGroup, TyperOption
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


def test_classify_outcome_outranks_stale_feedback_file() -> None:
    """Issue #78: a stale halt file left behind by a run that never resumed
    cleanly through `_finish_run` must not mask the durable outcome it
    recorded (e.g. a merged PR)."""
    run = runs.classify(
        73,
        worktree_path=None,
        live=None,
        has_feedback=True,
        pr=None,
        outcome=runs.Outcome("done", "https://x/pull/76", None),
    )
    assert run == runs.Run(73, "done", "PR #76 — https://x/pull/76")


def test_classify_live_outranks_outcome_record() -> None:
    """A live resume on an issue with a leftover outcome record must still
    report `running` — no regression for in-flight runs."""
    run = runs.classify(
        73,
        worktree_path=None,
        live=(1, "00:30", "implement"),
        has_feedback=False,
        pr=None,
        outcome=runs.Outcome("done", "https://x/pull/76", None),
    )
    assert run is not None
    assert run.state == "running"


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


def test_live_runs_matches_interleaved_project_option() -> None:
    """A hand-typed `agent implement --project /repo 77` puts the issue number
    after the option, not right after the verb — the shape a human typing by
    hand will produce sooner or later."""
    ps = "1 00:05 agent implement --project /repo 77\n"
    assert runs.live_runs(ps) == {77: (1, "00:05", "implement")}


def test_live_runs_matches_interleaved_project_equals_form() -> None:
    ps = "1 00:05 agent implement --project=/repo 77\n"
    assert runs.live_runs(ps) == {77: (1, "00:05", "implement")}


def test_live_runs_matches_interleaved_short_project_flag() -> None:
    ps = "1 00:05 agent implement -C /repo 77\n"
    assert runs.live_runs(ps) == {77: (1, "00:05", "implement")}


def test_live_runs_interleaved_project_still_filters_other_project(tmp_path: Path) -> None:
    """The interleaved scan must not break the existing --project filter."""
    other = tmp_path / "other-repo"
    other.mkdir()
    ps = f"1 00:05 agent implement --project {other} 77\n"
    assert runs.live_runs(ps, tmp_path / "agent-ops") == {}


def test_live_runs_interleaved_project_matches_when_same(tmp_path: Path) -> None:
    project = tmp_path / "agent-ops"
    ps = f"1 00:05 agent implement --project {project} 77\n"
    assert runs.live_runs(ps, project) == {77: (1, "00:05", "implement")}


def test_live_runs_message_flag_before_issue_is_ambiguous() -> None:
    """`--message`/`-m` take free text, which `ps` hands back as however many
    space-split tokens the message contains — there's no fixed value width to
    skip. `agent resume --message 77 73` can't tell a one-word message from
    an issue number that happens to come after it, so this must not guess
    `73` (or misreport the message word `77` as the issue)."""
    ps = "1 00:05 agent resume --message 77 73\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_short_message_flag_before_issue_is_ambiguous() -> None:
    ps = "1 00:05 agent resume -m 77 73\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_multiword_message_before_issue_does_not_phantom_match() -> None:
    """A quoted multi-word message survives `args.split()` as several tokens.
    Before the fix, the scan resumed inside the message text and returned the
    first bare digit it found there — a phantom match on the wrong issue."""
    ps = "1 00:05 agent resume -m retry 3 times 82\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_message_flag_after_issue_still_matches() -> None:
    """The documented usage (`docs/workflow.md`): issue number first, message
    last. The digit is found before the ambiguous flag is ever reached."""
    ps = '1 00:05 agent resume 82 -m "see comment 3"\n'
    assert runs.live_runs(ps) == {82: (1, "00:05", "resume")}


def test_live_runs_matches_shebang_expanded_argv_with_interleaved_options() -> None:
    ps = (
        "41233 06:12 /Users/x/.local/share/uv/tools/agent-ops/bin/python3 "
        "/Users/x/.local/bin/agent implement --project /repo 77\n"
    )
    assert runs.live_runs(ps) == {77: (41233, "06:12", "implement")}


def test_live_runs_excludes_plan() -> None:
    """`plan` has no worktree — see the `live_runs` docstring for why it's
    deliberately not a run verb."""
    ps = "1 00:05 agent plan 77\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_excludes_spec() -> None:
    """`spec` creates a detached worktree that never becomes a `discover_runs`
    candidate — see the `live_runs` docstring."""
    ps = "1 00:05 agent spec 77\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_excludes_review() -> None:
    """`review` is keyed by PR number, which would collide with issue keys."""
    ps = "1 00:05 agent review 45\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_excludes_groom() -> None:
    """`groom` takes no issue argument — nothing to key this dict on."""
    ps = "1 00:05 agent groom --project /repo\n"
    assert runs.live_runs(ps) == {}


def test_live_runs_excludes_scout() -> None:
    ps = "1 00:05 agent scout --max 3\n"
    assert runs.live_runs(ps) == {}


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

    result, trustworthy = runs.discover_runs(tmp_path)

    assert trustworthy is True
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

    result, trustworthy = runs.discover_runs(tmp_path)

    assert result == [runs.Run(68, "stopped", STOPPED_DETAIL)]
    assert trustworthy is True


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

    result, trustworthy = runs.discover_runs(tmp_path, log=warnings.append)

    assert result == [runs.Run(68, "stopped", STOPPED_DETAIL)]
    assert trustworthy is False
    assert any("could not list open PRs" in w for w in warnings)


def test_discover_runs_worktree_listing_failure_still_yields_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git worktree list` failing must degrade like the `gh` PR lookup does
    (empty + untrustworthy), not raise `CommandError` straight out of
    `discover_runs` and kill an hour-long wait over one transient hiccup."""
    (tmp_path / ".agent-runs").mkdir()
    (tmp_path / ".agent-runs" / "issue-68-feedback.md").write_text("findings")

    def boom(root: Path) -> list[worktree.Worktree]:
        raise CommandError("git worktree list: fatal: not a git repository")

    monkeypatch.setattr(runs.worktree, "list_worktrees", boom)
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    warnings: list[str] = []

    result, trustworthy = runs.discover_runs(tmp_path, log=warnings.append)

    assert result == [runs.Run(68, "halted", "self-review — resume with `agent resume 68`")]
    assert trustworthy is False
    assert any("could not list worktrees" in w for w in warnings)


def test_discover_runs_no_candidates_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    assert runs.discover_runs(tmp_path) == ([], True)


def _write_outcome_file(
    tmp_path: Path, issue: int, *, state: str = "done", pr_url: str | None = "https://x/pull/76"
) -> Path:
    runs_dir = tmp_path / ".agent-runs"
    runs_dir.mkdir(exist_ok=True)
    path = runs_dir / f"issue-{issue}-outcome.json"
    path.write_text(json.dumps({"state": state, "pr_url": pr_url, "reason": None}))
    return path


def test_discover_runs_outcome_only_reports_done_after_worktree_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #87: once the worktree/feedback/open-PR signals are all gone, the
    durable outcome record alone must still produce a `done` row."""
    _write_outcome_file(tmp_path, 42)
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result, trustworthy = runs.discover_runs(tmp_path)

    assert result == [runs.Run(42, "done", "PR #76 — https://x/pull/76")]
    # An outcome record is a local file read, not a `gh`/`git` call — finding
    # one never degrades the poll.
    assert trustworthy is True


def test_discover_runs_outcome_record_beats_stale_feedback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #78: a stale feedback file left behind alongside a later outcome
    record must not win — the run is `done`, not `halted` forever."""
    (tmp_path / ".agent-runs").mkdir()
    (tmp_path / ".agent-runs" / "issue-42-feedback.md").write_text("stale findings")
    _write_outcome_file(tmp_path, 42)
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result, _trustworthy = runs.discover_runs(tmp_path)

    assert result == [runs.Run(42, "done", "PR #76 — https://x/pull/76")]


def test_classify_outcome_over_feedback_is_only_safe_because_a_halt_clears_it(
    tmp_path: Path,
) -> None:
    """The precedence above is deliberate, and it is `implement._record_halt`'s
    unlink that keeps it honest (PR #93 review).

    Read this together with
    `test_a_new_cycles_halt_supersedes_the_previous_cycles_outcome_record` in
    tests/test_resume.py: `classify` has no way to tell a stale feedback file
    from a fresh one, so the write side must guarantee the two never coexist
    with the feedback being the newer of the pair. If that ever regresses,
    this row silently reads `done` while the run is waiting on
    `agent resume`.
    """
    outcome = runs.Outcome(state="done", pr_url="https://x/pull/76", reason=None)

    shadowed = runs.classify(
        42, worktree_path=None, live=None, has_feedback=True, pr=None, outcome=outcome
    )
    cleared = runs.classify(
        42, worktree_path=None, live=None, has_feedback=True, pr=None, outcome=None
    )

    assert shadowed == runs.Run(42, "done", "PR #76 — https://x/pull/76")
    assert cleared is not None and cleared.state == "halted"


def test_discover_runs_prunes_outcome_record_past_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_outcome_file(tmp_path, 42)
    old = runs.time.time() - runs._OUTCOME_TTL_S - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result, _trustworthy = runs.discover_runs(tmp_path)

    assert result == []
    assert not path.exists()


def test_discover_runs_invalid_outcome_json_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / ".agent-runs"
    runs_dir.mkdir()
    (runs_dir / "issue-42-outcome.json").write_text("not valid json")
    monkeypatch.setattr(runs.worktree, "list_worktrees", lambda root: [])
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    warnings: list[str] = []

    result, trustworthy = runs.discover_runs(tmp_path, log=warnings.append)

    assert result == []
    assert any("could not read outcome record" in w for w in warnings)
    # A corrupt outcome record is a local-file problem, not a degraded
    # `gh`/worktree poll: it must not push `wait_for_runs` toward its
    # degraded-streak bail-out.
    assert trustworthy is True


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
    """Patch `runs.discover_runs` to return `rounds` in order, one per call,
    each poll marked trustworthy (`True`).

    Calls beyond the given rounds keep returning the last one, so a wait loop
    that polls once more than expected doesn't crash — it just fails an
    assertion on the extra, unexpected log line instead.
    """
    it = iter(rounds)
    last: list = rounds[-1] if rounds else []

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        nonlocal last
        last = next(it, last)
        return last, True

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


def test_wait_for_runs_sees_done_via_outcome_record_instead_of_vanishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #87: once the worktree/feedback/PR signals are gone but a durable
    outcome record remains, `--wait` must see `done`, not the run disappearing
    (`gone`)."""
    _polls(
        monkeypatch,
        [runs.Run(77, "running", "pid 1")],
        [runs.Run(77, "done", "PR #76 — https://x/pull/76")],
    )
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert any("running → done" in line for line in lines)
    assert not any("gone" in line for line in lines)


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

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        log("warning: could not list open PRs (boom); runs may be misreported as stopped")
        # Trustworthy throughout: this test is specifically about the dedup
        # wrapper's once-only printing, not the new degraded-poll handling.
        if calls["n"] >= 3:
            return [runs.Run(77, "done", "PR #1")], True
        return [runs.Run(77, "running", "pid 1")], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert sum(1 for line in lines if line.startswith("warning:")) == 1


# --- wait_for_runs: gh/worktree outages (issue #86) ------------------------


def test_wait_for_runs_single_degraded_poll_resets_stopped_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single degraded poll reporting `stopped` must not count toward the
    2-poll debounce: recovering with genuine PR data on the next poll must
    still finish `done`, not a false `stopped` off the degraded observation."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [runs.Run(77, "stopped", STOPPED_DETAIL)], True
        if calls["n"] == 2:
            log("warning: could not list open PRs (boom); runs may be misreported as stopped")
            return [runs.Run(77, "stopped", STOPPED_DETAIL)], False
        return [runs.Run(77, "done", "PR #76")], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert calls["n"] == 3  # did not falsely terminate as `stopped` at poll 2
    assert any("→ done" in line for line in lines)


def test_wait_for_runs_untrustworthy_first_poll_does_not_seed_stopped_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untrustworthy poll that *establishes* the watch (the very first poll)
    must not seed `stopped_streak` as if it were a trustworthy observation.

    A degraded first poll reading `stopped` looks identical to the known
    dispatch pre-spawn race window. If it seeded a streak of 1, a single
    subsequent *trustworthy* poll that still reads `stopped` (the race still
    settling) would push the streak to 2 and end the wait after only one
    trustworthy `stopped` observation — violating the documented 2-consecutive-
    trustworthy-polls invariant. It must instead take a *second* trustworthy
    `stopped` poll to terminate."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            log("warning: could not list open PRs (boom); runs may be misreported as stopped")
            return [runs.Run(77, "stopped", STOPPED_DETAIL)], False
        # Both remaining polls are trustworthy and still read `stopped` — the
        # dispatch race settling slowly. Termination must wait for the second
        # of these, not the first.
        return [runs.Run(77, "stopped", STOPPED_DETAIL)], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    # poll 1: untrustworthy, establishes watch, streak must seed at 0 (not 1)
    # poll 2: trustworthy stopped -> streak 1 (not terminal)
    # poll 3: trustworthy stopped -> streak 2 (terminal)
    assert calls["n"] == 3


def test_wait_for_runs_raises_after_sustained_gh_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every poll degraded: this must surface as a distinct, non-zero, non-
    timeout failure once the degraded streak exceeds `_MAX_DEGRADED_POLLS`,
    rather than ever reporting a `stopped`/`gone` verdict built on unreliable
    data."""

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        log("warning: could not list open PRs (gh: rate limited); may misreport as stopped")
        return [runs.Run(77, "stopped", STOPPED_DETAIL)], False

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    with pytest.raises(CommandError) as exc_info:
        runs.wait_for_runs(tmp_path, log=lines.append)

    message = str(exc_info.value)
    assert "timed out" not in message
    assert "unreliable" in message
    assert "rate limited" in message  # names the last warning seen, per the plan


def test_wait_for_runs_worktree_listing_failure_mid_wait_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single bad `git worktree list` mid-wait must not crash the wait, nor
    mark the run `gone`: the previous state carries forward until a
    trustworthy poll confirms the real outcome."""
    wt_calls = {"n": 0}

    def list_worktrees(root: Path) -> list[worktree.Worktree]:
        wt_calls["n"] += 1
        if wt_calls["n"] == 2:
            raise CommandError("git worktree list: fatal: not a git repository")
        return [worktree.Worktree(tmp_path / ".worktrees" / "issue-68", "fix/issue-68")]

    monkeypatch.setattr(runs.worktree, "list_worktrees", list_worktrees)
    monkeypatch.setattr(runs, "_ps_output", lambda log: "")

    pr_calls = {"n": 0}

    def open_prs(cwd: Path) -> list[dict]:
        pr_calls["n"] += 1
        return [_pr(76, issue=68)] if pr_calls["n"] >= 2 else []

    monkeypatch.setattr(github, "open_prs", open_prs)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert not any("gone" in line for line in lines)
    assert any("stopped → done" in line for line in lines)
    assert any(line.startswith("note:") for line in lines)


def test_wait_for_runs_untrustworthy_disappearance_is_not_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watched issue missing from an untrustworthy poll must not be read as
    `gone` immediately — only a subsequent trustworthy poll may confirm it."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [runs.Run(77, "running", "pid 1")], True
        if calls["n"] == 2:
            log("warning: could not list worktrees (boom); runs may be misreported as stopped")
            return [], False
        return [], True  # trustworthy confirmation: genuinely gone

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert calls["n"] == 3  # the degraded poll (#2) did not end the wait early
    assert sum(1 for line in lines if "gone" in line) == 1


def test_wait_for_runs_logs_degradation_summary_on_clean_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wait that saw one degraded poll but still finished cleanly surfaces
    that in a final summary line — not just the single early `warning:` line
    that `_dedup_warnings` may have suppressed on every repeat since."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            log("warning: could not list open PRs (boom); runs may be misreported as stopped")
            return [runs.Run(77, "running", "pid 1")], False
        return [runs.Run(77, "done", "PR #1")], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert any(line.startswith("note:") and "1 of 2" in line for line in lines)


def test_wait_for_runs_first_poll_degraded_named_issue_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded *first* poll not showing the named issue must not be read as
    "no run found" — the watch set doesn't exist yet to hold state against,
    but the same "unknown is not evidence of death" reasoning still applies.
    Recovering with genuine data on the next poll must establish the watch and
    finish normally."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            log("warning: could not list worktrees (boom); runs may be misreported as stopped")
            return [], False
        if calls["n"] == 2:
            return [runs.Run(77, "running", "pid 1")], True
        return [runs.Run(77, "done", "PR #76")], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, issue=77, log=lines.append)

    assert result is True
    assert calls["n"] == 3  # did not raise "no run found" off the degraded first poll
    assert any("running → done" in line for line in lines)


def test_wait_for_runs_first_poll_degraded_watch_all_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded first poll with an empty result in watch-all mode must not
    be read as "no agent runs found" (exit 0) — the exact false-positive-
    success shape issue #86 was filed against, relocated to the
    watch-establishment step. Recovering on the next poll must establish the
    watch from real data and finish normally."""
    calls = {"n": 0}

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            log("warning: could not list worktrees (boom); runs may be misreported as stopped")
            return [], False
        if calls["n"] == 2:
            return [runs.Run(77, "running", "pid 1")], True
        return [runs.Run(77, "done", "PR #76")], True

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    result = runs.wait_for_runs(tmp_path, log=lines.append)

    assert result is True
    assert calls["n"] == 3  # did not exit early with "no agent runs found"
    assert not any("no agent runs found" in line for line in lines)
    assert any("running → done" in line for line in lines)


def test_wait_for_runs_raises_when_named_issue_degraded_from_first_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every poll from the very first one is degraded and never shows the
    named issue, this must still surface as the distinct degraded-outage
    `CommandError` (not a raised "no run found", and not a hang)."""

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        log("warning: could not list worktrees (gh: rate limited); may misreport as stopped")
        return [], False

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    with pytest.raises(CommandError) as exc_info:
        runs.wait_for_runs(tmp_path, issue=77, log=lines.append)

    message = str(exc_info.value)
    assert "no run found" not in message
    assert "unreliable" in message
    assert "rate limited" in message


def test_wait_for_runs_raises_when_watch_all_degraded_from_first_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above for watch-all mode: sustained degradation from the first
    poll must raise the degraded-outage `CommandError`, never silently
    conclude "no agent runs found" and exit 0."""

    def fake(project_root: Path, log=print) -> tuple[list, bool]:
        log("warning: could not list worktrees (gh: rate limited); may misreport as stopped")
        return [], False

    monkeypatch.setattr(runs, "discover_runs", fake)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    lines: list[str] = []

    with pytest.raises(CommandError) as exc_info:
        runs.wait_for_runs(tmp_path, log=lines.append)

    message = str(exc_info.value)
    assert "no agent runs found" not in message
    assert "unreliable" in message
    assert "rate limited" in message
    assert not any("no agent runs found" in line for line in lines)


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


def test_live_runs_attached_message_forms_do_not_phantom_match() -> None:
    """click accepts `--message=x` and `-mx`; `ps` shows the post-quoting argv.

    Matching only the detached spellings let these through, and the scan
    resumed inside the message text — returning a phantom issue number while
    reporting the real run as stopped. Both failures at once.
    """
    for args in (
        'agent resume --message="retry 3 times" 82',
        "agent resume -mretry 3 times 82",
    ):
        ps = f"1 00:05 /usr/bin/python3 /x/bin/agent {args.removeprefix('agent ')}\n"
        assert runs.live_runs(ps) == {}, args


def test_live_runs_still_finds_the_issue_after_a_valued_flag() -> None:
    """The guard must not swallow ordinary value-taking options."""
    ps = "1 00:05 /usr/bin/python3 /x/bin/agent implement --runtime codex 82\n"
    assert runs.live_runs(ps) == {82: (1, "00:05", "implement")}


# --- _VALUE_FLAGS mirrors the CLI -------------------------------------------


def _value_taking_flags(command_name: str) -> set[str]:
    """Every opts/secondary_opts spelling of a non-boolean Option on a typer command,
    excluding the free-text flags handled separately by `_is_free_text_flag`.

    Deliberately pinned to typer internals: since typer 0.27 typer vendors click
    as `typer._click` and no longer depends on the standalone `click` package, so
    there is no importable `click.Group`/`click.Option` to narrow against. The
    concrete classes typer builds the app out of are `typer.core.TyperGroup` and
    `typer.core.TyperOption` (the vendored `typer._click.core` exposes only the
    `Command`/`Parameter` bases, which carry neither `.commands` nor `.is_flag`).
    A typer major bump may move these again; that is the accepted tradeoff versus
    adding `click` back as a direct dependency just to introspect the CLI.

    `typer.main.get_command` is declared to return a bare `Command`, and
    `Parameter` (the declared element type of `cmd.params`) doesn't carry
    `.is_flag` — that's option-only. Both are narrowed with `isinstance` rather
    than trusted structurally, so this stays clean under pyright.

    Returning an empty set is treated as a bug in *this helper*, never as a fact
    about the CLI. Every assertion built on it is negative ("X must not be in the
    set"), so an empty set would let all of them pass while guarding nothing —
    precisely the silent drift #88 exists to catch. The realistic way that
    happens: `TyperOption` still imports fine but is no longer the class typer
    instantiates, so the `isinstance` filter below rejects every parameter.
    """
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)  # every typer command is nested under a group
    cmd = group.commands[command_name]
    flags: set[str] = set()
    for param in cmd.params:
        if not isinstance(param, TyperOption):  # skip the positional issue Argument
            continue
        if param.is_flag:  # boolean flags: `_find_issue` only skips the flag itself
            continue
        opts = set(param.opts) | set(param.secondary_opts)
        if opts & runs._FREE_TEXT_FLAGS:
            continue  # --message/-m: handled by _is_free_text_flag, not _VALUE_FLAGS
        flags.update(opts)
    assert flags, (
        f"{command_name}: introspection found no value-taking options at all. "
        f"{command_name} really does declare some, so this means the typer "
        f"internals this helper reads (TyperGroup/TyperOption) have moved and it "
        f"is no longer classifying anything — fix the helper rather than trusting "
        f"the green tests it would otherwise produce"
    )
    return flags


@pytest.mark.parametrize("command_name", ["implement", "resume", "dispatch"])
def test_value_flags_mirrors_cli(command_name: str) -> None:
    declared = _value_taking_flags(command_name)
    # Positive anchor before the negative check below: --project/-C is declared by
    # all three commands (the shared ProjectOpt), so finding it proves the helper
    # is really classifying options rather than returning some non-empty junk that
    # happens to satisfy `not missing`.
    assert {"--project", "-C"} <= declared, (
        f"{command_name}: --project/-C is a value-taking option on every one of "
        f"these commands but was not detected — the introspection is broken, so "
        f"the completeness check below would pass without checking anything"
    )
    missing = declared - runs._VALUE_FLAGS
    assert not missing, (
        f"{command_name}: {sorted(missing)} take a value in the CLI but are missing "
        f"from runs._VALUE_FLAGS — a phantom-issue-number bug waiting to happen"
    )


def test_value_flags_excludes_boolean_options() -> None:
    """Sanity check the classifier itself: known boolean flags must never be
    reported as value-taking, or the completeness test above would be vacuous.
    """
    implement_flags = _value_taking_flags("implement")
    assert "--force" not in implement_flags
    assert "--no-pr" not in implement_flags
    assert "--keep-worktree" not in implement_flags


def test_value_flags_free_text_message_not_required_in_value_flags() -> None:
    """--message/-m are deliberately absent from _VALUE_FLAGS (ambiguous width);
    the completeness check must not demand they be added.
    """
    resume_flags = _value_taking_flags("resume")
    assert "--message" not in resume_flags
    assert "-m" not in resume_flags
    assert {"--message", "-m"} <= runs._FREE_TEXT_FLAGS

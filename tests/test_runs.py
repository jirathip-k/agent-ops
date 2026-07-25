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

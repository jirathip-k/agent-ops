import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from agent_ops import github, messages, orca, runs, surfaces, worktree
from agent_ops.cli import app
from agent_ops.config import ProjectConfig
from agent_ops.loop import LoopOutcome
from agent_ops.runtimes.base import RunRequest, RunResult
from agent_ops.utils import CommandError, run
from agent_ops.workflows import implement as implement_module
from agent_ops.workflows.implement import SelfReview, run_implement, run_resume

runner = CliRunner()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


class FakeSurface:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Path, Path | None]] = []

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        self.calls.append((label, command, cwd, attach_path))
        return surfaces.Spawned(where="fake surface", surface=self.name)


def _fake_issue(number: int, cwd: Path) -> dict:
    return {"number": number, "title": "some bug", "body": "body", "labels": []}


# `_finish_run` only reads `.name` off `runtime` for PR-body attribution, and only
# when `open_pr=True`. Tests that exercise that path need a stand-in exposing it.
_fake_runtime = cast("Any", SimpleNamespace(name="fake"))


def test_resume_dispatches_to_the_existing_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-5", "fix/issue-5", "main")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(
        app, ["resume", "5", "--project", str(repo), "--message", "fix the thing"]
    )

    assert result.exit_code == 0
    root = repo.resolve()
    wt_path = root / ".worktrees" / "issue-5"
    ((label, command, cwd, attach_path),) = fake.calls
    assert label == "agent-resume-issue-5"
    assert command[:5] == ["agent", "resume", "5", "--surface", "inline"]
    assert cwd == root
    assert attach_path == wt_path


def test_resume_fails_clearly_when_worktree_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["resume", "42", "--project", str(repo)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    expected_path = repo.resolve() / ".worktrees" / "issue-42"
    stderr = result.stderr
    assert "#42" in stderr
    assert str(expected_path) in stderr
    assert fake.calls == []


def test_resume_message_travels_as_a_file_path_not_shell_text(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-6", "fix/issue-6", "main")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    message = "a 'quoted'\nmulti-line piece of feedback"

    result = runner.invoke(app, ["resume", "6", "--project", str(repo), "--message", message])

    assert result.exit_code == 0
    ((_, command, _, _),) = fake.calls
    assert "--message-file" in command
    feedback_path = Path(command[command.index("--message-file") + 1])
    assert feedback_path.is_absolute()
    assert feedback_path.read_text() == message
    assert not any(message in arg for arg in command)


def test_resume_rejects_message_and_message_file_together(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-7", "fix/issue-7", "main")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    message_file = repo / "feedback.md"
    message_file.write_text("feedback from a file")

    result = runner.invoke(
        app,
        [
            "resume",
            "7",
            "--project",
            str(repo),
            "--message",
            "inline feedback",
            "--message-file",
            str(message_file),
        ],
    )

    assert result.exit_code == 1
    assert fake.calls == []


def _fake_role_request(captured: dict[str, Any]):
    def fake_role_request(
        config: ProjectConfig,
        role_name: str,
        prompt: str,
        cwd: Path,
        *,
        runtime_override: str | None = None,
        extra_allowed_tools: tuple[str, ...] = (),
    ) -> tuple[object, RunRequest]:
        captured["role_name"] = role_name
        captured["prompt"] = prompt
        captured["cwd"] = cwd
        return object(), RunRequest(prompt=prompt, cwd=cwd)

    return fake_role_request


def test_run_resume_feeds_the_message_to_the_implementer_prompt(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = worktree.create(repo, ".worktrees", "issue-8", "fix/issue-8", "main")
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(implement_module, "role_request", _fake_role_request(captured))
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(False, 1, None, []),
    )

    ok = run_resume(repo, 8, message="please handle the edge case")

    assert ok is False  # loop outcome forced to fail — we only care about the prompt
    assert captured["role_name"] == "implementer"
    assert captured["cwd"] == wt_path
    assert "please handle the edge case" in captured["prompt"]


def test_run_resume_falls_back_to_the_stored_halt_feedback(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-9", "fix/issue-9", "main")
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    halt_path = repo / ".agent-runs" / "issue-9-feedback.md"
    halt_path.parent.mkdir(parents=True)
    halt_path.write_text("self-review found a missing test")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(implement_module, "role_request", _fake_role_request(captured))
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(False, 1, None, []),
    )

    run_resume(repo, 9)

    assert "self-review found a missing test" in captured["prompt"]


def test_run_resume_raises_clearly_when_no_feedback_is_available(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-10", "fix/issue-10", "main")
    monkeypatch.setattr(github, "get_issue", _fake_issue)

    with pytest.raises(FileNotFoundError, match="#10"):
        run_resume(repo, 10)


def test_run_resume_raises_when_worktree_is_missing(repo: Path) -> None:
    with pytest.raises(FileNotFoundError, match="#99"):
        run_resume(repo, 99)


def test_run_implement_halt_writes_feedback_file_and_comments_on_the_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    # Never reach a live `gh pr list`: utils.run raises FileNotFoundError when
    # gh isn't installed, so an unstubbed call errors instead of testing the
    # halt path (matches tests/test_implement_workflow.py).
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])
    monkeypatch.setattr(
        implement_module,
        "role_request",
        _fake_role_request({}),
    )
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
    )
    monkeypatch.setattr(
        implement_module,
        "_self_review",
        lambda *a, **k: SelfReview(False, "review found issues"),
    )
    comments: list[tuple[int, str]] = []
    monkeypatch.setattr(
        github, "comment_on_issue", lambda number, body, cwd: comments.append((number, body))
    )
    plan_file = repo / "plan.md"
    plan_file.write_text("approved plan")

    ok = run_implement(repo, 11, plan_file=plan_file, log=lambda _: None)

    assert ok is False
    feedback_path = repo / ".agent-runs" / "issue-11-feedback.md"
    assert feedback_path.read_text() == "review found issues"
    ((number, body),) = comments
    assert number == 11
    assert "review found issues" in body


def test_run_implement_halt_survives_a_failed_issue_comment(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    # Never reach a live `gh pr list`: utils.run raises FileNotFoundError when
    # gh isn't installed, so an unstubbed call errors instead of testing the
    # halt path (matches tests/test_implement_workflow.py).
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])
    monkeypatch.setattr(implement_module, "role_request", _fake_role_request({}))
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
    )
    monkeypatch.setattr(
        implement_module,
        "_self_review",
        lambda *a, **k: SelfReview(False, "review found issues"),
    )

    def failing_comment(number: int, body: str, cwd: Path) -> None:
        raise CommandError("no gh remote")

    monkeypatch.setattr(github, "comment_on_issue", failing_comment)
    plan_file = repo / "plan.md"
    plan_file.write_text("approved plan")

    ok = run_implement(repo, 12, plan_file=plan_file, log=lambda _: None)

    assert ok is False
    feedback_path = repo / ".agent-runs" / "issue-12-feedback.md"
    assert feedback_path.read_text() == "review found issues"


def test_empty_diff_halts_without_recording_findings_or_commenting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truly empty diff is "nothing to review", not a rejection.

    (Create-only runs are reviewed — see test_self_review_sees_untracked_files.)
    Recording this would post "changes requested — (empty diff — nothing to
    review)" on the issue and hand that string to the next run as feedback to
    address.
    """
    commented: list[int] = []
    monkeypatch.setattr(
        implement_module.github,
        "comment_on_issue",
        lambda number, body, cwd: commented.append(number),
    )
    monkeypatch.setattr(implement_module.orca, "report", lambda *a, **k: None)
    monkeypatch.setattr(
        implement_module,
        "_self_review",
        lambda *a, **k: implement_module.SelfReview(
            False, "(empty diff — nothing to review)", reviewed=False
        ),
    )

    config = ProjectConfig()
    assert config.loop.self_review  # the halt path only runs when it's enabled
    card = implement_module._CardReporter(tmp_path, tmp_path / "wt", lambda _: None)
    proceed = implement_module._review_and_maybe_halt(
        config, tmp_path, 40, tmp_path / "wt", card=card, runtime_name=None, log=lambda _: None
    )

    assert proceed is False
    assert commented == []
    assert not implement_module._feedback_path(tmp_path, 40).exists()


def test_ad_hoc_message_does_not_overwrite_the_halt_findings(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--message` must not destroy the stored self-review findings.

    Exercises dispatch_resume rather than comparing the two path helpers: a
    regression would put `--message` back into the halt file, and only a test
    that actually runs the code path can catch that.
    """
    halt = implement_module._feedback_path(repo, 5)
    halt.parent.mkdir(parents=True, exist_ok=True)
    halt.write_text("the real review findings")

    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    monkeypatch.setattr(
        implement_module, "_existing_worktree", lambda root, config, number: repo / "wt"
    )

    implement_module.dispatch_resume(repo, 5, message="also bump the version")

    assert halt.read_text() == "the real review findings"
    ((_, command, _, _),) = fake.calls
    passed = Path(command[command.index("--message-file") + 1])
    assert passed != halt
    assert passed.read_text() == "also bump the version"


def test_self_review_reports_nothing_to_review_for_an_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer of `reviewed=False`, not just its consumer.

    It early-returns before touching a runtime, so this is cheap — and without
    it the flag's only coverage monkeypatches the function that sets it.
    """
    monkeypatch.setattr(
        implement_module, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="")
    )

    review = implement_module._self_review(ProjectConfig(), tmp_path, log=lambda _: None)

    assert review.reviewed is False
    assert review.ok is False


def test_finish_run_clears_the_stored_findings_on_success(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale findings must not outlive the cycle that produced them.

    Lives in _finish_run rather than run_resume so a successful *implement*
    clears them too: otherwise a later `agent resume` on the same issue is
    handed a review that was already addressed, with no visible cause.
    """
    for path in (
        implement_module._feedback_path(repo, 7),
        implement_module._ad_hoc_message_path(repo, 7),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale findings from an earlier cycle")

    monkeypatch.setattr(implement_module.worktree, "remove", lambda *a, **k: None)
    monkeypatch.setattr(implement_module.orca, "report", lambda *a, **k: None)
    # _finish_run stages and commits in the worktree; this test is about what
    # happens after that, so the git calls are stubbed rather than staged.
    monkeypatch.setattr(
        implement_module,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="1 file changed"),
    )

    ok = implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        repo / "wt",
        RunRequest(prompt="p", cwd=repo / "wt"),
        cast("Any", None),  # only used for model attribution, never called here
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, repo / "wt", lambda _: None),
        open_pr=False,
        keep_worktree=False,
        log=lambda _: None,
    )

    assert ok is True
    assert not implement_module._feedback_path(repo, 7).exists()
    assert not implement_module._ad_hoc_message_path(repo, 7).exists()


def _stub_finish_run_git_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(implement_module.orca, "report", lambda *a, **k: None)
    monkeypatch.setattr(
        implement_module,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="1 file changed"),
    )


def test_finish_run_writes_outcome_record_with_pr_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #87: the durable record is what lets a finished run still show
    `done` once the worktree/feedback/open-PR signals it normally reads are
    gone."""
    _stub_finish_run_git_calls(monkeypatch)
    monkeypatch.setattr(implement_module.worktree, "remove", lambda *a, **k: None)
    monkeypatch.setattr(implement_module.github, "create_pr", lambda *a, **k: "https://x/pull/76")

    ok = implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        repo / "wt",
        RunRequest(prompt="p", cwd=repo / "wt"),
        _fake_runtime,
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, repo / "wt", lambda _: None),
        open_pr=True,
        keep_worktree=False,
        log=lambda _: None,
    )

    assert ok is True
    outcome_path = runs.outcome_path(repo, 7)
    assert outcome_path.is_file()
    data = json.loads(outcome_path.read_text())
    assert data["state"] == "done"
    assert data["pr_url"] == "https://x/pull/76"


def test_finish_run_writes_outcome_record_without_pr(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_finish_run_git_calls(monkeypatch)
    monkeypatch.setattr(implement_module.worktree, "remove", lambda *a, **k: None)

    ok = implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        repo / "wt",
        RunRequest(prompt="p", cwd=repo / "wt"),
        cast("Any", None),
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, repo / "wt", lambda _: None),
        open_pr=False,
        keep_worktree=False,
        log=lambda _: None,
    )

    assert ok is True
    data = json.loads(runs.outcome_path(repo, 7).read_text())
    assert data["state"] == "done"
    assert data["pr_url"] is None


def test_finish_run_outcome_survives_worktree_removal_and_discover_runs_reports_done(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: once `_finish_run` really removes the worktree, `agent runs`
    must still report `done` for the issue instead of no row at all."""
    monkeypatch.setattr(implement_module.orca, "report", lambda *a, **k: None)
    monkeypatch.setattr(
        implement_module,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="1 file changed"),
    )
    monkeypatch.setattr(implement_module.github, "create_pr", lambda *a, **k: "https://x/pull/76")

    wt_path = worktree.create(repo, ".worktrees", "issue-7", "fix/issue-7", "main")

    ok = implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        wt_path,
        RunRequest(prompt="p", cwd=wt_path),
        _fake_runtime,
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, wt_path, lambda _: None),
        open_pr=True,
        keep_worktree=False,
        log=lambda _: None,
    )
    assert ok is True
    assert not wt_path.exists()

    monkeypatch.setattr(github, "open_prs", lambda cwd: [])

    result, _trustworthy = runs.discover_runs(repo)

    assert result == [runs.Run(7, "done", "PR #76 — https://x/pull/76")]


def _existing_outcome_record(repo: Path, issue: int) -> Path:
    """A `done` outcome record from an earlier, already-finished cycle."""
    path = runs.outcome_path(repo, issue)
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"state": "done", "pr_url": "https://x/pull/76", "reason": None}),
    )
    return path


def test_a_new_cycles_halt_supersedes_the_previous_cycles_outcome_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #93 review: `classify` ranks `outcome` above `has_feedback`, so an
    outcome record nothing ever cleared would shadow a later cycle's halt and
    report `done  PR #76` — the user would never be told to `agent resume`."""
    outcome_path = _existing_outcome_record(repo, 11)
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])
    monkeypatch.setattr(github, "comment_on_issue", lambda number, body, cwd: None)
    monkeypatch.setattr(implement_module, "role_request", _fake_role_request({}))
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
    )
    monkeypatch.setattr(
        implement_module,
        "_self_review",
        lambda *a, **k: SelfReview(False, "review found issues"),
    )
    plan_file = repo / "plan.md"
    plan_file.write_text("approved plan")

    ok = run_implement(repo, 11, plan_file=plan_file, log=lambda _: None)

    assert ok is False
    assert not outcome_path.exists()
    assert (repo / ".agent-runs" / "issue-11-feedback.md").read_text() == "review found issues"

    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    result, _trustworthy = runs.discover_runs(repo)

    (row,) = result
    assert row.issue == 11
    assert row.state == "halted"
    assert "agent resume 11" in row.detail


def test_record_halt_clears_the_outcome_record_even_when_the_unlink_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clear is best-effort like the rest of `_record_halt`: an outcome
    record that refuses to delete must still leave the halt recorded."""
    _existing_outcome_record(repo, 13)

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    logged: list[str] = []
    monkeypatch.setattr(github, "comment_on_issue", lambda number, body, cwd: None)

    implement_module._record_halt(repo, 13, "review found issues", log=logged.append)

    assert (repo / ".agent-runs" / "issue-13-feedback.md").read_text() == "review found issues"
    assert any("could not clear stale outcome record" in line for line in logged)


def test_starting_a_new_run_clears_the_previous_cycles_outcome_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate-failure exit writes neither a feedback file nor a record of its
    own — it relies on the kept worktree reporting `stopped`. A stale `done`
    from the previous cycle would mask that, so the record is dropped when the
    cycle starts, not only when it halts."""
    outcome_path = _existing_outcome_record(repo, 14)
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])
    monkeypatch.setattr(implement_module, "role_request", _fake_role_request({}))
    monkeypatch.setattr(
        implement_module,
        "run_task_loop",
        lambda *a, **k: LoopOutcome(False, 3, RunResult(ok=False, text="nope"), []),
    )
    plan_file = repo / "plan.md"
    plan_file.write_text("approved plan")

    ok = run_implement(repo, 14, plan_file=plan_file, log=lambda _: None)

    assert ok is False
    assert not outcome_path.exists()

    monkeypatch.setattr(runs, "_ps_output", lambda log: "")
    monkeypatch.setattr(github, "open_prs", lambda cwd: [])
    result, _trustworthy = runs.discover_runs(repo)

    (row,) = result
    assert row.issue == 14
    assert row.state == "stopped"


def test_an_open_pr_bail_out_leaves_the_outcome_record_alone(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_implement` returning early because a PR is already open starts no
    cycle, so it has no business discarding the previous one's record."""
    outcome_path = _existing_outcome_record(repo, 15)
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(
        github, "open_prs_for_issue", lambda number, cwd: [{"number": 76, "url": "https://x/76"}]
    )

    ok = run_implement(repo, 15, log=lambda _: None)

    assert ok is False
    assert outcome_path.exists()


def test_self_review_sees_untracked_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An implementer that only creates files must still be reviewed.

    `git diff` alone reports nothing for untracked files, so without the
    intent-to-add pass this run would look empty and skip review entirely —
    the common shape for "add X" issues. Delete that line and this test fails.
    """
    (repo / "new_module.py").write_text("def added(): ...\n")
    captured: dict[str, str] = {}

    def fake_role_request(config, role_name, prompt, cwd, **kwargs):
        captured["prompt"] = prompt
        return object(), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(implement_module, "role_request", fake_role_request)
    monkeypatch.setattr(
        implement_module,
        "run_with_fallback",
        lambda runtime, request, on_event=None: RunResult(ok=True, text="APPROVE"),
    )

    review = implement_module._self_review(ProjectConfig(), repo, log=lambda _: None)

    assert review.reviewed is True
    assert "new_module.py" in captured["prompt"]


# --- pushed outcomes (issue #98) ------------------------------------------


def _sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture what this run would push, without a message bus behind it."""
    sends: list[dict] = []

    def fake(project_root, issue, *, state, pr_url=None, reason=None, log=lambda _: None) -> bool:
        sends.append({"issue": issue, "state": state, "pr_url": pr_url, "reason": reason})
        return True

    monkeypatch.setattr(implement_module.messages, "send_outcome", fake)
    return sends


def test_finish_run_reports_done_with_the_same_facts_as_the_durable_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_finish_run_git_calls(monkeypatch)
    monkeypatch.setattr(implement_module.worktree, "remove", lambda *a, **k: None)
    monkeypatch.setattr(implement_module.github, "create_pr", lambda *a, **k: "https://x/pull/76")
    sends = _sent(monkeypatch)

    implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        repo / "wt",
        RunRequest(prompt="p", cwd=repo / "wt"),
        _fake_runtime,
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, repo / "wt", lambda _: None),
        open_pr=True,
        keep_worktree=False,
        log=lambda _: None,
    )

    assert sends == [{"issue": 7, "state": "done", "pr_url": "https://x/pull/76", "reason": None}]
    durable = json.loads(runs.outcome_path(repo, 7).read_text())
    assert sends[0]["state"] == durable["state"]
    assert sends[0]["pr_url"] == durable["pr_url"]


def test_finish_run_reports_only_after_the_cleanup_it_announces(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor takes a pushed `done` as terminal on the spot, so the
    report must come after the worktree removal, not before it."""
    _stub_finish_run_git_calls(monkeypatch)
    monkeypatch.setattr(implement_module.github, "create_pr", lambda *a, **k: "https://x/pull/76")
    order: list[str] = []
    monkeypatch.setattr(
        implement_module.worktree, "remove", lambda *a, **k: order.append("worktree removed")
    )
    monkeypatch.setattr(
        implement_module.messages,
        "send_outcome",
        lambda *a, **k: order.append("reported") or True,
    )

    implement_module._finish_run(
        repo,
        ProjectConfig(),
        _fake_issue(7, repo),
        7,
        "issue-7",
        "fix/issue-7",
        repo / "wt",
        RunRequest(prompt="p", cwd=repo / "wt"),
        _fake_runtime,
        LoopOutcome(True, 1, RunResult(ok=True, text="done"), []),
        card=implement_module._CardReporter(repo, repo / "wt", lambda _: None),
        open_pr=True,
        keep_worktree=False,
        log=lambda _: None,
    )

    assert order == ["worktree removed", "reported"]


def test_record_halt_reports_halted_so_blocked_is_not_read_as_finished(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(implement_module.github, "comment_on_issue", lambda *a, **k: None)
    sends = _sent(monkeypatch)

    implement_module._record_halt(repo, 13, "review found issues", log=lambda _: None)

    assert len(sends) == 1
    assert sends[0]["state"] == "halted"
    assert "agent resume 13" in (sends[0]["reason"] or "")


def test_record_halt_survives_a_message_bus_that_has_gone_away(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort throughout, exercised through the real `messages` code: the
    findings must still be stashed and the comment still posted even when the
    push cannot happen at all."""
    commented: list[int] = []
    monkeypatch.setattr(
        implement_module.github,
        "comment_on_issue",
        lambda number, body, cwd: commented.append(number),
    )
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "executable", lambda: "orca")
    messages.record_spawn(repo, 13, surface="orca", handle="term_abc")

    def vanished(cmd: list[str], **kwargs: object) -> None:
        raise FileNotFoundError("orca: no such file")

    monkeypatch.setattr(messages, "run", vanished)
    logged: list[str] = []

    implement_module._record_halt(repo, 13, "review found issues", log=logged.append)

    assert implement_module._feedback_path(repo, 13).read_text() == "review found issues"
    assert commented == [13]
    assert any("could not push" in line for line in logged)


def test_review_with_nothing_to_review_reports_rather_than_vanishing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(implement_module.orca, "report", lambda *a, **k: None)
    monkeypatch.setattr(
        implement_module,
        "_self_review",
        lambda *a, **k: implement_module.SelfReview(
            False, "(empty diff — nothing to review)", reviewed=False
        ),
    )
    sends = _sent(monkeypatch)

    card = implement_module._CardReporter(repo, repo / "wt", lambda _: None)
    proceed = implement_module._review_and_maybe_halt(
        ProjectConfig(), repo, 40, repo / "wt", card=card, runtime_name=None, log=lambda _: None
    )

    assert proceed is False
    assert sends[0]["state"] == "failed"
    assert "nothing to review" in (sends[0]["reason"] or "")


def test_dispatch_resume_records_the_new_terminal_as_the_mailbox(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume terminal owns the issue now, so it is the handle a
    supervisor should be watching."""
    messages.record_spawn(repo, 5, surface="orca", handle="term_original")
    halt = implement_module._feedback_path(repo, 5)
    halt.parent.mkdir(parents=True, exist_ok=True)
    halt.write_text("findings")
    monkeypatch.setattr(
        implement_module, "_existing_worktree", lambda root, config, number: repo / "wt"
    )

    class RecordingSurface:
        name = "orca"

        def available(self) -> bool:
            return True

        def spawn(
            self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
        ) -> surfaces.Spawned:
            return surfaces.Spawned(where="w", surface="orca", handle="term_resumed")

    monkeypatch.setattr(surfaces, "pick", lambda name="auto": RecordingSurface())

    implement_module.dispatch_resume(repo, 5, log=lambda _: None)

    record = messages.load_spawn(repo, 5)
    assert record is not None
    assert record.handle == "term_resumed"

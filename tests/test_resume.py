import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_ops import github, surfaces, worktree
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
    ) -> str:
        self.calls.append((label, command, cwd, attach_path))
        return "fake surface"


def _fake_issue(number: int, cwd: Path) -> dict:
    return {"number": number, "title": "some bug", "body": "body", "labels": []}


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
    """An empty diff is "nothing to review", not a rejection.

    `git diff` ignores untracked files, so an implementer that only creates
    files produces one. Recording it would post "changes requested — (empty
    diff — nothing to review)" on the issue and hand that string to the next
    run as feedback to address.
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
    proceed = implement_module._review_and_maybe_halt(
        config, tmp_path, 40, tmp_path / "wt", runtime_name=None, log=lambda _: None
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

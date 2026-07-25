from pathlib import Path

import pytest

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.workflows import spec as spec_mod
from agent_ops.workflows.spec import run_spec


class _FakeRuntime:
    name = "fake"

    def __init__(self, text: str = "## Agent spec\n\nSummary text", ok: bool = True) -> None:
        self.text = text
        self.ok = ok
        self.received_prompt: str | None = None

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.received_prompt = request.prompt
        return RunResult(ok=self.ok, text=self.text)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


class _RaisingRuntime:
    name = "fake"

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        raise RuntimeError("boom mid-run")


def _stub_role_request(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        return runtime, RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(spec_mod, "role_request", fake_role_request)


def _stub_issue(monkeypatch: pytest.MonkeyPatch, issue_number: int = 7) -> None:
    monkeypatch.setattr(
        github,
        "get_issue",
        lambda number, cwd: {
            "number": issue_number,
            "title": "some idea",
            "body": "elaborate on this",
            "labels": [],
        },
    )


def _stub_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, bool]]:
    removed: list[tuple[str, bool]] = []

    def fake_remove(
        project_root: Path,
        worktree_dir: str,
        task_id: str,
        *,
        force: bool = False,
        delete_branch: bool = False,
    ) -> None:
        removed.append((task_id, force))

    monkeypatch.setattr(
        worktree,
        "create_detached",
        lambda project_root, worktree_dir, name, ref: tmp_path / "wt",
    )
    monkeypatch.setattr(worktree, "remove", fake_remove)
    return removed


def _stub_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spec_mod, "run", lambda *args, **kwargs: None)


def _stub_comments(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    posted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        github, "comment_on_issue", lambda number, body, cwd: posted.append((number, body))
    )
    return posted


def test_run_spec_happy_path_posts_comment_and_removes_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_issue(monkeypatch, issue_number=7)
    _stub_fetch(monkeypatch)
    removed = _stub_worktree(monkeypatch, tmp_path)
    posted = _stub_comments(monkeypatch)
    runtime = _FakeRuntime(text="## Summary\n\nDoes the thing")
    _stub_role_request(monkeypatch, runtime)

    result = run_spec(tmp_path, 7)

    assert result == "## Summary\n\nDoes the thing"
    assert len(posted) == 1
    number, body = posted[0]
    assert number == 7
    assert body.startswith("## Agent spec")
    assert "_agent-ops · model:" in body
    assert removed == [("spec-7-tmp", True)]
    assert runtime.received_prompt is not None
    assert "#7" in runtime.received_prompt
    assert "some idea" in runtime.received_prompt
    assert "elaborate on this" in runtime.received_prompt


def test_run_spec_post_false_returns_text_without_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_issue(monkeypatch)
    _stub_fetch(monkeypatch)
    removed = _stub_worktree(monkeypatch, tmp_path)
    posted = _stub_comments(monkeypatch)
    runtime = _FakeRuntime(text="## Summary\n\nDoes the thing")
    _stub_role_request(monkeypatch, runtime)

    result = run_spec(tmp_path, 7, post=False)

    assert result == "## Summary\n\nDoes the thing"
    assert posted == []
    assert removed == [("spec-7-tmp", True)]


@pytest.mark.parametrize("escalation_text", ["ESCALATE: needs a human", "  escalate: lowercase"])
def test_run_spec_escalate_raises_and_posts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escalation_text: str
) -> None:
    _stub_issue(monkeypatch)
    _stub_fetch(monkeypatch)
    removed = _stub_worktree(monkeypatch, tmp_path)
    posted = _stub_comments(monkeypatch)
    runtime = _FakeRuntime(text=escalation_text)
    _stub_role_request(monkeypatch, runtime)

    with pytest.raises(RuntimeError, match="escalated"):
        run_spec(tmp_path, 7)

    assert posted == []
    assert removed == [("spec-7-tmp", True)]


def test_run_spec_failed_run_raises_and_posts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_issue(monkeypatch)
    _stub_fetch(monkeypatch)
    removed = _stub_worktree(monkeypatch, tmp_path)
    posted = _stub_comments(monkeypatch)
    runtime = _FakeRuntime(text="couldn't finish", ok=False)
    _stub_role_request(monkeypatch, runtime)

    with pytest.raises(RuntimeError, match="Spec run failed"):
        run_spec(tmp_path, 7)

    assert posted == []
    assert removed == [("spec-7-tmp", True)]


def test_run_spec_runtime_exception_still_removes_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_issue(monkeypatch)
    _stub_fetch(monkeypatch)
    removed = _stub_worktree(monkeypatch, tmp_path)
    _stub_comments(monkeypatch)
    _stub_role_request(monkeypatch, _RaisingRuntime())

    with pytest.raises(RuntimeError, match="boom mid-run"):
        run_spec(tmp_path, 7)

    assert removed == [("spec-7-tmp", True)]

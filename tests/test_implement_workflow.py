from pathlib import Path

import pytest

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.workflows.implement import gate_allowed_tools, run_implement


def test_gate_allowed_tools_covers_each_command() -> None:
    config = ProjectConfig.model_validate(
        {
            "commands": {
                "setup": "npm install",
                "test": "uv run pytest -q",
                "typecheck": "uv run pyright",
            }
        }
    )
    patterns = gate_allowed_tools(config)
    assert "Bash(npm install)" in patterns
    assert "Bash(uv run pytest -q)" in patterns
    assert "Bash(uv run pytest -q:*)" in patterns
    assert "Bash(uv run pyright)" in patterns


def test_gate_allowed_tools_splits_compound_commands() -> None:
    config = ProjectConfig.model_validate(
        {"commands": {"lint": "uv run ruff check . && uv run ruff format --check ."}}
    )
    patterns = gate_allowed_tools(config)
    assert "Bash(uv run ruff check .)" in patterns
    assert "Bash(uv run ruff format --check .)" in patterns


def test_gate_allowed_tools_empty_when_no_commands() -> None:
    assert gate_allowed_tools(ProjectConfig()) == ()


def _fake_issue(number: int, cwd: Path) -> dict:
    return {"number": number, "title": "some bug", "body": "body", "labels": []}


def test_run_implement_refuses_when_open_pr_already_references_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(
        github,
        "open_prs_for_issue",
        lambda number, cwd: [
            {"number": 141, "url": "https://github.com/org/repo/pull/141", "headRefName": ""}
        ],
    )

    def fail_create(*args: object, **kwargs: object) -> Path:
        raise AssertionError("worktree.create should not be called when the guard trips")

    monkeypatch.setattr(worktree, "create", fail_create)

    messages: list[str] = []
    ok = run_implement(tmp_path, 132, log=messages.append)

    assert ok is False
    assert not (tmp_path / ".worktrees" / "issue-132").exists()
    assert any("#132" in m and "#141" in m for m in messages)


def test_run_implement_force_skips_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "get_issue", _fake_issue)

    def fail_open_prs(*args: object, **kwargs: object) -> list[dict]:
        raise AssertionError("open_prs_for_issue should not be called when force=True")

    monkeypatch.setattr(github, "open_prs_for_issue", fail_open_prs)

    class _ReachedWorktreeCreate(Exception):
        pass

    def fake_create(*args: object, **kwargs: object) -> Path:
        raise _ReachedWorktreeCreate

    monkeypatch.setattr(worktree, "create", fake_create)

    with pytest.raises(_ReachedWorktreeCreate):
        run_implement(tmp_path, 132, force=True)

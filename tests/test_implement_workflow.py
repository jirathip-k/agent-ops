from pathlib import Path

import pytest

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.runtimes.base import RunRequest, RunResult
from agent_ops.workflows import implement as implement_module
from agent_ops.workflows.implement import (
    _format_comments,
    gate_allowed_tools,
    make_plan,
    run_implement,
)


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


def test_format_comments_returns_sentinel_when_missing_or_empty() -> None:
    assert _format_comments({}) == "(no comments)"
    assert _format_comments({"comments": []}) == "(no comments)"


def test_format_comments_falls_back_to_unknown_author() -> None:
    issue = {"comments": [{"author": None, "createdAt": "2024-01-01T00:00:00Z", "body": "hi"}]}
    text = _format_comments(issue)
    assert "unknown" in text
    assert "hi" in text


def test_format_comments_preserves_spec_comment_verbatim() -> None:
    issue = {
        "comments": [
            {
                "author": {"login": "agent-ops-bot"},
                "createdAt": "2024-01-01T00:00:00Z",
                "body": "## Agent spec\n\nsome elaborated details",
            }
        ]
    }
    text = _format_comments(issue)
    assert "## Agent spec\n\nsome elaborated details" in text


def test_format_comments_pins_spec_comment_beyond_recency_cutoff() -> None:
    leading_filler = [
        {
            "author": {"login": f"early{i}"},
            "createdAt": f"2024-01-{i + 1:02d}",
            "body": f"filler {i}",
        }
        for i in range(2)
    ]
    spec_comment = {
        "author": {"login": "agent-ops-bot"},
        "createdAt": "2024-01-03T00:00:00Z",
        "body": "## Agent spec\n\nEARLY SPEC MARKER",
    }
    trailing_filler = [
        {
            "author": {"login": f"late{i}"},
            "createdAt": f"2024-02-{i + 1:02d}",
            "body": f"filler {i}",
        }
        for i in range(25)
    ]
    comments = leading_filler + [spec_comment] + trailing_filler
    issue = {"comments": comments}

    # sanity-check the fixture actually exercises the bug: more than the cap,
    # and the spec comment falls outside a pure `comments[-20:]` tail slice.
    assert len(comments) > 20
    assert spec_comment not in comments[-20:]

    text = _format_comments(issue)
    assert "EARLY SPEC MARKER" in text


def test_format_comments_preserves_chronological_order() -> None:
    issue = {
        "comments": [
            {"author": {"login": "a"}, "createdAt": "1", "body": "first comment"},
            {"author": {"login": "b"}, "createdAt": "2", "body": "second comment"},
        ]
    }
    text = _format_comments(issue)
    assert text.index("first comment") < text.index("second comment")


def test_make_plan_includes_issue_comments_in_rendered_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = {
        "number": 29,
        "title": "some bug",
        "body": "body",
        "labels": [],
        "comments": [
            {
                "author": {"login": "agent-ops-bot"},
                "createdAt": "2024-01-01T00:00:00Z",
                "body": "## Agent spec\n\nbuild on this instead of re-deriving",
            }
        ],
    }
    captured: dict[str, str] = {}

    def fake_role_request(
        config: ProjectConfig,
        role_name: str,
        prompt: str,
        cwd: Path,
        *,
        runtime_override: str | None = None,
        extra_allowed_tools: tuple[str, ...] = (),
    ) -> tuple[object, RunRequest]:
        captured["prompt"] = prompt
        return object(), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(implement_module, "role_request", fake_role_request)
    monkeypatch.setattr(
        implement_module,
        "run_with_fallback",
        lambda runtime, request, on_event=None: RunResult(ok=True, text="a plan"),
    )

    make_plan(ProjectConfig(), issue, tmp_path)

    assert "## Agent spec" in captured["prompt"]
    assert "build on this instead of re-deriving" in captured["prompt"]

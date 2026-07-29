"""`commands.requires`: the platform resolves what a repo says its gates need.

The failure this guards is issue #246 — a gate dying on `<tool>: not found`
after a full plan+implement cycle, indistinguishable from bad code.
"""

import os
import stat
from pathlib import Path

import pytest

from agent_ops import gates, github
from agent_ops.config import ProjectConfig
from agent_ops.gates import format_missing_requirements, missing_requirements
from agent_ops.utils import run
from agent_ops.workflows import implement as implement_module
from agent_ops.workflows.implement import run_implement


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _write_config(repo: Path, body: str) -> None:
    (repo / ".agent").mkdir(exist_ok=True)
    (repo / ".agent" / "config.yaml").write_text(body)


def _executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho ok\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_issue(number: int, cwd: Path) -> dict:
    return {"number": number, "title": "some bug", "body": "body", "labels": []}


def _no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])


def test_requires_defaults_to_empty() -> None:
    assert ProjectConfig().commands.requires == []


def test_empty_requires_spawns_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("an empty `requires` must not resolve anything")

    monkeypatch.setattr(gates, "run", fail_run)

    assert missing_requirements(ProjectConfig(), tmp_path) == []


def test_missing_requirements_names_only_what_does_not_resolve(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {"commands": {"requires": ["sh", "definitely-not-a-real-binary-246"]}}
    )

    assert missing_requirements(config, tmp_path) == ["definitely-not-a-real-binary-246"]


def test_missing_requirements_resolves_relative_to_the_given_directory(tmp_path: Path) -> None:
    """A worktree-local path (the `node_modules/.bin` shape) resolves there and only there."""
    project = tmp_path / "project"
    _executable(project / "node_modules" / ".bin" / "hugo")
    config = ProjectConfig.model_validate({"commands": {"requires": ["./node_modules/.bin/hugo"]}})

    assert missing_requirements(config, project) == []
    assert missing_requirements(config, tmp_path) == ["./node_modules/.bin/hugo"]


def test_format_missing_requirements_names_the_binary_the_gates_and_the_setup_command(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {"commands": {"setup": "npm ci", "test": "npm run check", "requires": ["hugo"]}}
    )

    message = format_missing_requirements(config, ["hugo"], tmp_path)

    assert "hugo" in message
    assert "npm run check" in message
    assert "npm ci" in message
    assert str(tmp_path / ".agent" / "config.yaml") in message
    assert "not a code failure" in message


def test_run_implement_aborts_before_the_planner_when_a_required_binary_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_github(monkeypatch)
    _write_config(
        repo,
        "commands:\n"
        '  setup: "true"\n'
        '  test: "npm run check"\n'
        "  requires: [definitely-not-a-real-binary-246]\n",
    )

    def fail_plan(*args: object, **kwargs: object) -> object:
        raise AssertionError("the planner must not start on an unprovisioned toolchain")

    monkeypatch.setattr(implement_module, "make_plan", fail_plan)
    monkeypatch.setattr(implement_module, "role_request", fail_plan)

    messages: list[str] = []
    ok = run_implement(repo, 246, log=messages.append)

    assert ok is False
    output = "\n".join(messages)
    assert "definitely-not-a-real-binary-246" in output
    assert "npm run check" in output
    assert str(repo / ".agent" / "config.yaml") in output
    assert not (repo / ".worktrees" / "issue-246").exists()  # aborted like a setup failure


def test_run_implement_passes_a_binary_that_only_exists_on_path_after_setup(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool is absent when the run starts and present because `setup` installed it."""
    _no_github(monkeypatch)
    prefix = tmp_path / "prefix" / "bin"
    prefix.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{prefix}{os.pathsep}{os.environ['PATH']}")
    source = tmp_path / "hugo-246"
    _executable(source)
    _write_config(
        repo,
        "commands:\n"
        f"""  setup: "cp '{source}' '{prefix / "hugo-246"}'"\n"""
        '  test: "npm run check"\n'
        "  requires: [hugo-246]\n",
    )
    assert missing_requirements(
        ProjectConfig.model_validate({"commands": {"requires": ["hugo-246"]}}), repo
    ) == ["hugo-246"]

    class _ReachedPlanner(Exception):
        pass

    def reached(*args: object, **kwargs: object) -> object:
        raise _ReachedPlanner

    monkeypatch.setattr(implement_module, "make_plan", reached)

    with pytest.raises(_ReachedPlanner):
        run_implement(repo, 247, log=lambda _: None)


def test_run_implement_with_no_requires_reaches_the_planner_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_github(monkeypatch)
    _write_config(repo, 'commands:\n  setup: "true"\n  test: "npm run check"\n')

    class _ReachedPlanner(Exception):
        pass

    def reached(*args: object, **kwargs: object) -> object:
        raise _ReachedPlanner

    monkeypatch.setattr(implement_module, "make_plan", reached)

    def fail_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("nothing should be resolved when `requires` is empty")

    monkeypatch.setattr(gates, "run", fail_run)

    with pytest.raises(_ReachedPlanner):
        run_implement(repo, 248, log=lambda _: None)

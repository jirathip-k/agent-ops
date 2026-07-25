from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import cli, surfaces
from agent_ops.cli import app
from agent_ops.utils import CommandError

runner = CliRunner()


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


class FailingSurface:
    name = "failing"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
        raise CommandError("spawn exploded")


def _record_inline(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int, str | None]]:
    """Stub the inline plan workflow; return the list it records calls into."""
    calls: list[tuple[Path, int, str | None]] = []

    class FakeResult:
        text = "PLAN: do the thing"

    def fake_make_plan(config, issue_data, cwd, *, runtime_override=None, log=None):
        calls.append((cwd, issue_data["number"], runtime_override))
        return None, FakeResult()

    def fake_get_issue(issue, cwd):
        return {"number": issue, "title": "t", "body": "b"}

    monkeypatch.setattr(cli, "make_plan", fake_make_plan)
    monkeypatch.setattr(cli.github, "get_issue", fake_get_issue)
    monkeypatch.setattr(cli, "load_project_config", lambda root: object())
    return calls


def test_plan_defaults_to_inline_and_prints_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["plan", "45", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "PLAN: do the thing" in result.output
    assert calls == [(tmp_path.resolve(), 45, None)]
    assert fake.calls == []  # default must never touch a surface


def test_plan_on_a_surface_spawns_the_command_and_echoes_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["plan", "45", "--project", str(tmp_path), "--surface", "auto"])

    assert result.exit_code == 0
    assert calls == []  # spawned, not run in-process
    ((label, command, cwd, attach_path),) = fake.calls
    assert label == "agent-plan-issue-45"
    assert command == ["agent", "plan", "45", "--project", str(tmp_path.resolve())]
    assert cwd == tmp_path.resolve()
    assert attach_path is None  # no task worktree for a read-only plan
    assert "dispatched plan for issue #45" in result.output
    assert "fake surface" in result.output


def test_plan_surface_forwards_post_and_runtime_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(
        app,
        [
            "plan",
            "45",
            "--project",
            str(tmp_path),
            "--surface",
            "orca",
            "--post",
            "--runtime",
            "codex",
        ],
    )

    assert result.exit_code == 0
    ((_, command, _, _),) = fake.calls
    assert command == [
        "agent",
        "plan",
        "45",
        "--post",
        "--runtime",
        "codex",
        "--project",
        str(tmp_path.resolve()),
    ]


def test_plan_surface_name_is_passed_through_to_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)
    picked: list[str] = []
    fake = FakeSurface()

    def fake_pick(name: str = "auto") -> FakeSurface:
        picked.append(name)
        return fake

    monkeypatch.setattr(surfaces, "pick", fake_pick)

    result = runner.invoke(
        app, ["plan", "45", "--project", str(tmp_path), "--surface", "background"]
    )

    assert result.exit_code == 0
    assert picked == ["background"]


def test_plan_unknown_surface_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)

    result = runner.invoke(app, ["plan", "45", "--project", str(tmp_path), "--surface", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.stderr
    assert calls == []  # an unknown surface must not silently fall back to inline


def test_plan_reports_spawn_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record_inline(monkeypatch)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())

    result = runner.invoke(app, ["plan", "45", "--project", str(tmp_path), "--surface", "auto"])

    assert result.exit_code == 1
    assert "spawn exploded" in result.stderr


def test_plan_surface_auto_falls_back_to_background_when_orca_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_ops.workflows import implement

    monkeypatch.setattr(surfaces.OrcaSurface, "available", lambda self: False)
    assert isinstance(surfaces.pick("auto"), surfaces.BackgroundSurface)

    monkeypatch.setattr(implement, "plan_command", lambda *args, **kwargs: ["true"])

    where = implement.dispatch_plan(tmp_path, 45, surface_name="auto")

    log_path = tmp_path / ".agent-runs" / "agent-plan-issue-45.log"
    assert log_path.exists()
    assert "background pid" in where

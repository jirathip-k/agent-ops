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


def _record_inline(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int, str | None, bool]]:
    """Stub the inline review workflow; return the list it records calls into."""
    calls: list[tuple[Path, int, str | None, bool]] = []

    def fake_run_review(
        project_root: Path,
        pr_number: int,
        *,
        runtime_name: str | None = None,
        post_comment: bool = False,
    ) -> str:
        calls.append((project_root, pr_number, runtime_name, post_comment))
        return "APPROVE: looks fine"

    monkeypatch.setattr(cli, "run_review", fake_run_review)
    return calls


def test_review_defaults_to_inline_and_prints_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["review", "45", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "APPROVE: looks fine" in result.output
    assert calls == [(tmp_path.resolve(), 45, None, False)]
    assert fake.calls == []  # default must never touch a surface


def test_review_on_a_surface_spawns_the_command_and_echoes_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["review", "45", "--project", str(tmp_path), "--surface", "auto"])

    assert result.exit_code == 0
    assert calls == []  # spawned, not run in-process
    ((label, command, cwd, attach_path),) = fake.calls
    assert label == "agent-review-pr-45"
    assert command == ["agent", "review", "45", "--project", str(tmp_path.resolve())]
    assert cwd == tmp_path.resolve()
    assert attach_path is None  # no task worktree for a read-only review
    assert "fake surface" in result.output


def test_review_surface_forwards_post_and_runtime_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(
        app,
        [
            "review",
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
        "review",
        "45",
        "--post",
        "--runtime",
        "codex",
        "--project",
        str(tmp_path.resolve()),
    ]


def test_review_surface_name_is_passed_through_to_pick(
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
        app, ["review", "45", "--project", str(tmp_path), "--surface", "background"]
    )

    assert result.exit_code == 0
    assert picked == ["background"]


def test_review_unknown_surface_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_inline(monkeypatch)

    result = runner.invoke(app, ["review", "45", "--project", str(tmp_path), "--surface", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.stderr
    assert calls == []  # an unknown surface must not silently fall back to inline


def test_review_reports_spawn_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record_inline(monkeypatch)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())

    result = runner.invoke(app, ["review", "45", "--project", str(tmp_path), "--surface", "auto"])

    assert result.exit_code == 1
    assert "spawn exploded" in result.stderr

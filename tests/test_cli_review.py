from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import cli, github, surfaces
from agent_ops.cli import app
from agent_ops.utils import CommandError
from agent_ops.workflows.review import ReviewOutcome

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


# --- multi-PR fan-out --------------------------------------------------


def test_review_no_prs_and_no_all_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)

    result = runner.invoke(app, ["review", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "--all" in result.stderr


def test_review_prs_and_all_together_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)

    result = runner.invoke(app, ["review", "1", "2", "--all", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "not both" in result.stderr


def test_review_all_resolves_open_prs_against_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_open_pr_numbers(base: str, cwd: Path) -> list[int]:
        calls.append((base, cwd))
        return [7]

    monkeypatch.setattr(github, "open_pr_numbers", fake_open_pr_numbers)
    inline_calls = _record_inline(monkeypatch)

    result = runner.invoke(app, ["review", "--all", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert calls == [("main", tmp_path.resolve())]
    assert inline_calls == [(tmp_path.resolve(), 7, None, False)]


def test_review_all_with_no_open_prs_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "open_pr_numbers", lambda base, cwd: [])
    inline_calls = _record_inline(monkeypatch)

    result = runner.invoke(app, ["review", "--all", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert inline_calls == []


def test_review_multi_pr_surface_spawns_one_command_with_all_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_inline(monkeypatch)
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(
        app, ["review", "1", "2", "3", "--project", str(tmp_path), "--surface", "auto"]
    )

    assert result.exit_code == 0
    ((label, command, cwd, attach_path),) = fake.calls
    assert label == "agent-review-3-prs"
    assert command == [
        "agent",
        "review",
        "1",
        "2",
        "3",
        "--project",
        str(tmp_path.resolve()),
    ]
    assert cwd == tmp_path.resolve()


def test_review_multi_pr_inline_prints_each_review_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, list[int]]] = []

    def fake_run_reviews(
        project_root: Path,
        pr_numbers: list[int],
        *,
        jobs: int = 3,
        post_comment: bool = False,
        runtime_name: str | None = None,
        log: object = None,
    ) -> list[ReviewOutcome]:
        calls.append((project_root, pr_numbers))
        return [
            ReviewOutcome(1, "approve", text="VERDICT: APPROVE"),
            ReviewOutcome(2, "request_changes", text="VERDICT: REQUEST CHANGES"),
        ]

    monkeypatch.setattr(cli, "run_reviews", fake_run_reviews)

    result = runner.invoke(app, ["review", "1", "2", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert calls == [(tmp_path.resolve(), [1, 2])]
    assert "VERDICT: APPROVE" in result.output
    assert "VERDICT: REQUEST CHANGES" in result.output
    assert "PR #1: APPROVE" in result.output
    assert "PR #2: REQUEST CHANGES" in result.output


def test_review_multi_pr_exits_nonzero_when_any_outcome_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_reviews(
        project_root: Path,
        pr_numbers: list[int],
        *,
        jobs: int = 3,
        post_comment: bool = False,
        runtime_name: str | None = None,
        log: object = None,
    ) -> list[ReviewOutcome]:
        return [
            ReviewOutcome(1, "approve", text="VERDICT: APPROVE"),
            ReviewOutcome(2, "failed", error="boom"),
        ]

    monkeypatch.setattr(cli, "run_reviews", fake_run_reviews)

    result = runner.invoke(app, ["review", "1", "2", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "PR #2: run failed" in result.output


def test_review_multi_pr_all_request_changes_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_reviews(
        project_root: Path,
        pr_numbers: list[int],
        *,
        jobs: int = 3,
        post_comment: bool = False,
        runtime_name: str | None = None,
        log: object = None,
    ) -> list[ReviewOutcome]:
        return [
            ReviewOutcome(1, "request_changes", text="VERDICT: REQUEST CHANGES"),
            ReviewOutcome(2, "request_changes", text="VERDICT: REQUEST CHANGES"),
        ]

    monkeypatch.setattr(cli, "run_reviews", fake_run_reviews)

    result = runner.invoke(app, ["review", "1", "2", "--project", str(tmp_path)])

    assert result.exit_code == 0

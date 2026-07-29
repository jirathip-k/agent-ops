from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import cli
from agent_ops.cli import app

runner = CliRunner()


def test_check_flag_exits_zero_on_clean_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_merge_check", lambda root, pr, **kw: [])
    monkeypatch.setattr(
        cli,
        "run_merge",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("--check must not merge")),
    )

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path), "--check"])

    assert result.exit_code == 0


def test_check_flag_exits_nonzero_on_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_merge_check", lambda root, pr, **kw: ["blocked: too big"])
    monkeypatch.setattr(
        cli,
        "run_merge",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("--check must not merge")),
    )

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path), "--check"])

    assert result.exit_code == 1


def test_check_and_override_together_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("neither run_merge nor run_merge_check should run")

    monkeypatch.setattr(cli, "run_merge_check", _boom)
    monkeypatch.setattr(cli, "run_merge", _boom)

    result = runner.invoke(
        app, ["merge", "45", "--project", str(tmp_path), "--check", "--override"]
    )

    assert result.exit_code == 1


def test_check_and_force_together_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("neither run_merge nor run_merge_check should run")

    monkeypatch.setattr(cli, "run_merge_check", _boom)
    monkeypatch.setattr(cli, "run_merge", _boom)

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path), "--check", "--force"])

    assert result.exit_code == 1


def test_force_flag_is_passed_through_to_run_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_merge(root: Path, pr: int, **kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(cli, "run_merge", fake_run_merge)

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert captured["force"] is True


def test_default_invocation_passes_force_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_merge(root: Path, pr: int, **kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(cli, "run_merge", fake_run_merge)

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["force"] is False


# ---------- --batch / --all-clean (issue #272) ------------------------------


def test_batch_without_prs_or_all_clean_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("nothing should run")

    monkeypatch.setattr(cli, "run_merge", _boom)
    monkeypatch.setattr(cli, "run_merge_batch", _boom)
    monkeypatch.setattr(cli, "batch_candidates", _boom)

    result = runner.invoke(app, ["merge", "--project", str(tmp_path), "--batch"])

    assert result.exit_code == 1


def test_all_clean_with_explicit_pr_numbers_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("nothing should run")

    monkeypatch.setattr(cli, "run_merge", _boom)
    monkeypatch.setattr(cli, "run_merge_batch", _boom)
    monkeypatch.setattr(cli, "batch_candidates", _boom)

    result = runner.invoke(
        app, ["merge", "45", "--project", str(tmp_path), "--batch", "--all-clean"]
    )

    assert result.exit_code == 1


def test_all_clean_without_batch_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("nothing should run")

    monkeypatch.setattr(cli, "run_merge", _boom)
    monkeypatch.setattr(cli, "run_merge_batch", _boom)
    monkeypatch.setattr(cli, "batch_candidates", _boom)

    result = runner.invoke(app, ["merge", "--project", str(tmp_path), "--all-clean"])

    assert result.exit_code == 1


def test_bare_multi_pr_without_batch_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --batch, more than one PR number is refused rather than only
    acting on the first — the same shape as `--batch` being required at all
    for more than a lone `agent merge`."""

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("nothing should run without --batch for more than one PR")

    monkeypatch.setattr(cli, "run_merge", _boom)
    monkeypatch.setattr(cli, "run_merge_batch", _boom)

    result = runner.invoke(app, ["merge", "45", "46", "--project", str(tmp_path)])

    assert result.exit_code == 1


def test_check_and_batch_together_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("neither run_merge, run_merge_check nor run_merge_batch should run")

    monkeypatch.setattr(cli, "run_merge_check", _boom)
    monkeypatch.setattr(cli, "run_merge", _boom)
    monkeypatch.setattr(cli, "run_merge_batch", _boom)

    result = runner.invoke(app, ["merge", "45", "--project", str(tmp_path), "--check", "--batch"])

    assert result.exit_code == 1


def test_batch_flag_calls_run_merge_batch_with_pr_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_merge_batch(root: Path, pr_numbers: list[int], **kwargs: object) -> bool:
        captured["pr_numbers"] = pr_numbers
        captured.update(kwargs)
        return True

    monkeypatch.setattr(cli, "run_merge_batch", fake_run_merge_batch)

    result = runner.invoke(app, ["merge", "10", "20", "30", "--project", str(tmp_path), "--batch"])

    assert result.exit_code == 0
    assert captured["pr_numbers"] == [10, 20, 30]


def test_batch_flag_reports_failure_as_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_merge_batch", lambda *a, **k: False)

    result = runner.invoke(app, ["merge", "10", "20", "--project", str(tmp_path), "--batch"])

    assert result.exit_code == 1


def test_all_clean_uses_batch_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "batch_candidates", lambda root: [7, 8])
    captured: dict[str, object] = {}

    def fake_run_merge_batch(root: Path, pr_numbers: list[int], **kwargs: object) -> bool:
        captured["pr_numbers"] = pr_numbers
        return True

    monkeypatch.setattr(cli, "run_merge_batch", fake_run_merge_batch)

    result = runner.invoke(app, ["merge", "--project", str(tmp_path), "--batch", "--all-clean"])

    assert result.exit_code == 0
    assert captured["pr_numbers"] == [7, 8]


def test_all_clean_with_no_candidates_exits_0_without_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "batch_candidates", lambda root: [])
    monkeypatch.setattr(
        cli,
        "run_merge_batch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing to batch")),
    )

    result = runner.invoke(app, ["merge", "--project", str(tmp_path), "--batch", "--all-clean"])

    assert result.exit_code == 0

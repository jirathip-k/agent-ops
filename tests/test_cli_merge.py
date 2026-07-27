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

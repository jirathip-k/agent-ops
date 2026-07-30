from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops.cli import app
from agent_ops.config import load_project_config
from agent_ops.runtimes import credentials
from agent_ops.utils import run
from agent_ops.workflows.implement import role_request

runner = CliRunner()


def _write_config(root: Path, text: str) -> None:
    config = root / ".agent" / "config.yaml"
    config.parent.mkdir()
    config.write_text(text)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture(autouse=True)
def _clear_captured_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_runtime_env", {})


def _hybrid_config() -> str:
    return """\
runtime:
  name: claude_code
  stream: false
model_tiers:
  claude_code:
    smart: claude-smart-fixture
    fast: claude-fast-fixture
  codex:
    smart: codex-smart-fixture
    fast: codex-fast-fixture
agents:
  planner:
    runtime: claude_code
    model: smart
  implementer:
    runtime: codex
    model: fast
  reviewer:
    runtime: claude_code
    model: smart
"""


def test_capture_unlinks_token_and_scopes_each_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "claude-token"
    token_file.write_text("anthropic-secret")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    credentials.capture_ci_credentials()

    assert not token_file.exists()
    assert "AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE" not in os.environ
    assert "AGENT_CODEX_HOME" not in os.environ
    claude_env = credentials.environment_for("claude_code")
    codex_env = credentials.environment_for("codex")
    assert claude_env is not None
    assert codex_env is not None
    assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "anthropic-secret"
    assert "CODEX_HOME" not in claude_env
    assert codex_env["CODEX_HOME"] == str(codex_home)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in codex_env

    # Repository setup/gates inherit the scrubbed parent, not either runtime child.
    proc = run(
        [
            "sh",
            "-c",
            'test -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && test -z "${CODEX_HOME:-}"',
        ],
        check=False,
    )
    assert proc.returncode == 0


def test_hybrid_fixture_resolves_and_executes_each_role_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, _hybrid_config())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "claude",
        """#!/bin/sh
printf 'claude|%s|token=%s\n' "$*" "$CLAUDE_CODE_OAUTH_TOKEN" >> "$CALLS"
printf '{"result":"done","is_error":false}\n'
""",
    )
    _write_executable(
        fake_bin / "codex",
        """#!/bin/sh
printf 'codex|%s|home=%s\n' "$*" "$CODEX_HOME" >> "$CALLS"
printf 'done\n'
""",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CALLS", str(calls))
    token_file = tmp_path / "claude-token"
    token_file.write_text("anthropic-secret")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_CODEX_HOME", str(codex_home))
    credentials.capture_ci_credentials()

    config = load_project_config(tmp_path)
    for role in ("planner", "implementer", "reviewer"):
        runtime, request = role_request(config, role, f"{role} prompt", tmp_path)
        result = runtime.run(request)
        assert result.ok

    lines = calls.read_text().splitlines()
    assert [line.split("|", 1)[0] for line in lines] == ["claude", "codex", "claude"]
    assert all("--model claude-smart-fixture" in line for line in (lines[0], lines[2]))
    assert "--model codex-fast-fixture" in lines[1]
    assert lines[0].endswith("token=anthropic-secret")
    assert lines[1].endswith(f"home={codex_home}")


def test_runtime_preflight_names_missing_role_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, _hybrid_config())
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "claude" else None,
    )

    result = runner.invoke(app, ["runtime-preflight", "-C", str(tmp_path)])

    assert result.exit_code == 1
    assert "implementer: codex / codex-fast-fixture" in result.output
    assert "codex CLI missing (required by configured role implementer)" in result.output


def test_runtime_preflight_names_missing_model_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _hybrid_config().replace(
        "  codex:\n    smart: codex-smart-fixture\n    fast: codex-fast-fixture\n",
        "",
    )
    _write_config(tmp_path, config)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    result = runner.invoke(app, ["runtime-preflight", "-C", str(tmp_path)])

    assert result.exit_code == 1
    assert "implementer" in result.output
    assert "runtime 'codex' has no model for tier 'fast'" in result.output

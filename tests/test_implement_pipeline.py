"""Executable guards for the manual hybrid implementation lane (#296)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from agent_ops.utils import PLATFORM_ROOT, run

_PIPELINE_PATH = PLATFORM_ROOT / ".github" / "workflows" / "implement-pipeline.yml"
_STUB_PATH = PLATFORM_ROOT / "stubs" / "managed-repo-implement.yml"
_PIPELINE = yaml.safe_load(_PIPELINE_PATH.read_text())
_JOB = _PIPELINE["jobs"]["implement"]
_STEPS = _JOB["steps"]
_STEPS_BY_NAME = {step["name"]: step for step in _STEPS}


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _run_script(script: str, tmp_path: Path, env: dict[str, str]) -> tuple[int, str]:
    proc = run(
        ["bash", "-c", script],
        cwd=tmp_path,
        check=False,
        env={**os.environ, **env},
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_dispatch_requires_target_repo_and_exact_issue() -> None:
    # PyYAML 1.1 parses the bare `on` key as True.
    triggers = _PIPELINE[True]
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert dispatch["target_repo"]["required"] is True
    assert dispatch["issue"] == {
        "description": "Exact GitHub issue number to implement",
        "required": True,
        "type": "number",
    }
    called = triggers["workflow_call"]["inputs"]
    assert called["target_repo"]["required"] is True
    assert called["issue"]["required"] is True


def test_stub_is_manual_only_and_requires_issue() -> None:
    stub = yaml.safe_load(_STUB_PATH.read_text())
    triggers = stub[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["issue"]["required"] is True
    assert triggers["workflow_dispatch"]["inputs"]["issue"]["type"] == "number"


def test_pipeline_uses_target_runtime_config_without_global_override() -> None:
    script = _STEPS_BY_NAME["Implement exact issue"]["run"]
    assert 'agent implement "$ISSUE"' in script
    assert "--runtime" not in script
    assert _JOB["concurrency"]["group"] == "agent-triage-${{ inputs.target_repo }}"


def test_codex_action_is_proxy_only_and_raw_key_is_not_job_wide() -> None:
    proxy = _STEPS_BY_NAME["Start protected Codex proxy"]
    assert proxy["uses"] == "openai/codex-action@v1"
    assert proxy["with"]["safety-strategy"] == "drop-sudo"
    assert "openai-api-key" in proxy["with"]
    assert "prompt" not in proxy["with"]
    assert "env" not in _JOB
    for step in _STEPS:
        if step["name"] in {"Validate provider credentials", "Start protected Codex proxy"}:
            continue
        assert "OPENAI_API_KEY" not in step.get("env", {})


def test_credential_validation_shell_fails_with_named_diagnostics(tmp_path: Path) -> None:
    script = _STEPS_BY_NAME["Validate provider credentials"]["run"]

    code, output = _run_script(
        script,
        tmp_path,
        {"CLAUDE_CODE_OAUTH_TOKEN": "", "OPENAI_API_KEY": "openai"},
    )
    assert code == 1
    assert "Missing Anthropic credential" in output

    code, output = _run_script(
        script,
        tmp_path,
        {"CLAUDE_CODE_OAUTH_TOKEN": "claude", "OPENAI_API_KEY": ""},
    )
    assert code == 1
    assert "Missing OpenAI credential" in output


def test_every_new_shell_block_executes_against_fixture_commands(tmp_path: Path) -> None:
    """Execute every `run:` artifact; plausible YAML text is not a guard."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "npm",
        '#!/bin/sh\nprintf "npm:%s\\n" "$*" >> "$CALLS"\n',
    )
    _write_executable(
        fake_bin / "uv",
        """#!/bin/sh
printf 'uv:%s\n' "$*" >> "$CALLS"
if [ "${1:-}" = "run" ] && [ "${3:-}" = "implement" ]; then
  test -z "${CLAUDE_CODE_OAUTH_TOKEN:-}"
  test -f "$AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE"
  test "$(cat "$AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE")" = "claude-fixture"
  rm "$AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE"
fi
""",
    )
    env = {
        "AGENT_CODEX_HOME": str(tmp_path / "codex-home"),
        "CALLS": str(calls),
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-fixture",
        "GITHUB_WORKSPACE": str(tmp_path),
        "ISSUE": "296",
        "OPENAI_API_KEY": "openai-fixture",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
    }

    run_steps = [step for step in _STEPS if "run" in step]
    assert {step["name"] for step in run_steps} == {
        "Validate provider credentials",
        "Install Claude Code CLI",
        "Preflight resolved runtime fleet",
        "Implement exact issue",
    }
    for step in run_steps:
        code, output = _run_script(step["run"], tmp_path, env)
        assert code == 0, f"{step['name']} failed:\n{output}"

    logged = calls.read_text().splitlines()
    assert "npm:install -g @anthropic-ai/claude-code" in logged
    assert "uv:run agent runtime-preflight -C " + str(tmp_path / "target") in logged
    implement = next(line for line in logged if "agent implement" in line)
    assert implement == f"uv:run agent implement 296 -C {tmp_path / 'target'}"
    assert "--runtime" not in implement

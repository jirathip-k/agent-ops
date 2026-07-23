from pathlib import Path

from agent_ops.config import load_project_config


def test_defaults_load_without_project_config(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)
    assert config.runtime.name == "claude_code"
    assert config.base_branch == "main"
    assert config.loop.max_attempts >= 1
    assert config.commands.test is None


def test_project_config_overrides_defaults(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "base_branch: develop\ncommands:\n  test: uv run pytest -q\nloop:\n  max_attempts: 5\n"
    )
    config = load_project_config(tmp_path)
    assert config.base_branch == "develop"
    assert config.commands.test == "uv run pytest -q"
    assert config.loop.max_attempts == 5
    # untouched keys keep platform defaults
    assert config.runtime.name == "claude_code"
    assert config.commands.lint is None

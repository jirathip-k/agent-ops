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


def test_platform_defaults_tier_models_by_role(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)
    planner = config.resolve_role("planner")
    implementer = config.resolve_role("implementer")
    reviewer = config.resolve_role("reviewer")

    # planner and reviewer get the smart model, read-only
    assert planner.model == "opus"
    assert planner.permission_mode == "plan"
    assert reviewer.model == "opus"
    assert reviewer.permission_mode == "plan"
    # implementer inherits the base runtime (default model, write access)
    assert implementer.model is None
    assert implementer.permission_mode == "acceptEdits"
    # all roles share the base runtime unless overridden
    assert {planner.runtime, implementer.runtime, reviewer.runtime} == {"claude_code"}


def test_role_overrides_fall_back_to_base_runtime(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "runtime:\n"
        "  model: sonnet\n"
        "  max_turns: 40\n"
        "agents:\n"
        "  implementer:\n"
        "    runtime: codex\n"
        "  reviewer:\n"
        "    model: haiku\n"
    )
    config = load_project_config(tmp_path)

    implementer = config.resolve_role("implementer")
    assert implementer.runtime == "codex"
    assert implementer.model == "sonnet"  # inherited from base runtime
    assert implementer.max_turns == 40

    reviewer = config.resolve_role("reviewer")
    assert reviewer.runtime == "claude_code"
    assert reviewer.model == "haiku"
    assert reviewer.permission_mode == "plan"  # platform default kept

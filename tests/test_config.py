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

    # planner and reviewer get the smart tier (fable), effectively read-only
    # (default mode: reads auto-allowed, writes denied headless)
    assert planner.model == "fable"
    assert planner.permission_mode == "default"
    assert reviewer.model == "fable"
    assert reviewer.permission_mode == "default"
    # implementer runs the fast tier (sonnet) with write access
    assert implementer.model == "sonnet"
    assert implementer.permission_mode == "acceptEdits"
    # all roles share the base runtime unless overridden
    assert {planner.runtime, implementer.runtime, reviewer.runtime} == {"claude_code"}


def test_model_tiers_map_per_runtime(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "model_tiers:\n"
        "  claude_code:\n"
        "    smart: my-pinned-model\n"
        "agents:\n"
        "  implementer:\n"
        "    runtime: codex\n"
        "    model: smart\n"
    )
    config = load_project_config(tmp_path)
    # project tier override wins for claude_code roles
    assert config.resolve_role("planner").model == "my-pinned-model"
    # codex has no tier table → the name passes through untouched
    assert config.resolve_role("implementer").model == "smart"
    # non-tier names are never rewritten
    assert config.model_tiers["claude_code"].get("fast") == "sonnet"


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
        "    model: null\n"
        "  reviewer:\n"
        "    model: haiku\n"
    )
    config = load_project_config(tmp_path)

    implementer = config.resolve_role("implementer")
    assert implementer.runtime == "codex"
    assert implementer.model == "sonnet"  # role model cleared → inherited from base runtime
    assert implementer.max_turns == 40

    reviewer = config.resolve_role("reviewer")
    assert reviewer.runtime == "claude_code"
    assert reviewer.model == "haiku"
    assert reviewer.permission_mode == "default"  # platform default kept

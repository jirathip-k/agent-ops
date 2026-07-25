from pathlib import Path

import pytest

from agent_ops.config import ModelTierError, load_project_config, role_reports


def _write_config(tmp_path: Path, body: str) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "config.yaml").write_text(body)


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
    _write_config(
        tmp_path,
        "model_tiers:\n"
        "  claude_code:\n"
        "    smart: my-pinned-model\n"
        "  codex:\n"
        "    smart: gpt-5-codex\n"
        "agents:\n"
        "  implementer:\n"
        "    runtime: codex\n"
        "    model: smart\n",
    )
    config = load_project_config(tmp_path)
    # project tier override wins for claude_code roles
    assert config.resolve_role("planner").model == "my-pinned-model"
    # a role on codex resolves against codex's own table
    assert config.resolve_role("implementer").model == "gpt-5-codex"
    # non-tier names are never rewritten
    assert config.model_tiers["claude_code"].get("fast") == "sonnet"


def test_runtime_override_resolves_tiers_against_the_overridden_runtime(tmp_path: Path) -> None:
    """Regression for #39: `--runtime codex` must not hand codex a Claude model."""
    _write_config(
        tmp_path,
        "model_tiers:\n  codex:\n    smart: gpt-5-codex\n    fast: gpt-5-codex-mini\n",
    )
    config = load_project_config(tmp_path)

    assert config.resolve_role("reviewer").model == "fable"  # unchanged without an override
    assert config.resolve_role("reviewer", runtime_override="codex").model == "gpt-5-codex"
    assert config.resolve_role("implementer", runtime_override="codex").model == "gpt-5-codex-mini"
    assert config.resolve_role("reviewer", runtime_override="codex").runtime == "codex"


def test_missing_tier_for_the_effective_runtime_raises(tmp_path: Path) -> None:
    """Better a clear error here than a foreign model name reaching the CLI."""
    config = load_project_config(tmp_path)  # defaults define claude_code tiers only

    with pytest.raises(ModelTierError) as exc:
        config.resolve_role("reviewer", runtime_override="codex")

    message = str(exc.value)
    assert "codex" in message and "smart" in message


def test_concrete_models_are_not_treated_as_tiers(tmp_path: Path) -> None:
    _write_config(tmp_path, "agents:\n  reviewer:\n    runtime: codex\n    model: gpt-5-codex\n")
    config = load_project_config(tmp_path)
    # not a tier name anywhere, so it passes through even without a codex table
    assert config.resolve_role("reviewer").model == "gpt-5-codex"


def test_no_fallbacks_configured_by_default(tmp_path: Path) -> None:
    """The mechanism ships inert: defaults.yaml carries no ladder."""
    config = load_project_config(tmp_path)
    assert config.model_fallbacks == {}
    assert all(config.resolve_role(role).fallbacks == [] for role in ("planner", "reviewer"))


def test_project_can_configure_a_fallback_ladder(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_fallbacks:\n"
        "  claude_code:\n"
        "    smart: [fable, opus, sonnet]\n"
        "    fast: [sonnet, haiku]\n",
    )
    config = load_project_config(tmp_path)

    # the ladder is the full sequence; the active model and rungs above it drop out
    assert config.resolve_role("reviewer").fallbacks == ["opus", "sonnet"]
    assert config.resolve_role("implementer").fallbacks == ["haiku"]


def test_ladder_for_a_pinned_model_outside_it_is_used_whole(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_tiers:\n  claude_code:\n    smart: my-pinned-model\n"
        "model_fallbacks:\n  claude_code:\n    smart: [opus, sonnet]\n",
    )
    config = load_project_config(tmp_path)
    assert config.resolve_role("planner").fallbacks == ["opus", "sonnet"]


def test_ladder_rungs_may_name_tiers(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_fallbacks:\n  claude_code:\n    smart: [smart, fast]\n")
    config = load_project_config(tmp_path)
    assert config.resolve_role("planner").fallbacks == ["sonnet"]


def test_role_reports_cover_every_role_and_surface_errors(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_fallbacks:\n  claude_code:\n    smart: [fable, opus]\n"
        "agents:\n  implementer:\n    runtime: codex\n",
    )
    reports = {r.name: r for r in role_reports(load_project_config(tmp_path))}

    assert reports["reviewer"].model == "fable"
    assert reports["reviewer"].fallbacks == ["opus"]
    # implementer wants the `fast` tier on codex, which has no table at all
    assert reports["implementer"].runtime == "codex"
    assert reports["implementer"].error is not None
    assert "fast" in reports["implementer"].error


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

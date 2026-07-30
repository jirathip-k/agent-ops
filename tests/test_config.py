from pathlib import Path

import pydantic
import pytest

from agent_ops import config as config_mod
from agent_ops.config import (
    ROLE_NAMES,
    ModelTierError,
    RuntimeChainConfigError,
    ladder_warnings,
    load_project_config,
    role_reports,
    runtime_reports,
)


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


def test_distill_config_defaults(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)
    assert config.distill.min_lines == 200
    assert config.distill.protected_sections == [
        "What this project is",
        "Architecture",
        "Conventions",
        "Commands",
        "Danger zones",
        "Maintaining this file",
    ]


def test_distill_config_project_override_wins(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "distill:\n  min_lines: 50\n  protected_sections: [Overview]\n",
    )
    config = load_project_config(tmp_path)
    assert config.distill.min_lines == 50
    assert config.distill.protected_sections == ["Overview"]


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


def test_gate_timeout_defaults_from_platform_and_is_overridable(tmp_path: Path) -> None:
    assert load_project_config(tmp_path).loop.gate_timeout_seconds == 1800
    _write_config(tmp_path, "loop:\n  gate_timeout_seconds: 300\n")
    config = load_project_config(tmp_path)
    assert config.loop.gate_timeout_seconds == 300
    assert config.loop.max_attempts == 3  # sibling loop keys keep their defaults


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


def test_interactive_sessions_default_to_a_mode_that_never_stops_to_ask(tmp_path: Path) -> None:
    """A spawned worker has nobody to answer a prompt, so it must not raise one (#115)."""
    config = load_project_config(tmp_path)
    assert config.runtime.interactive_permission_mode == "bypassPermissions"
    # ...and the headless path is untouched by that: it has the loop watching it.
    assert config.runtime.permission_mode == "acceptEdits"


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


def test_ordered_runtimes_resolve_each_provider_tier_and_ladder(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_tiers:\n"
        "  codex:\n"
        "    smart: gpt-smart\n"
        "    fast: gpt-fast\n"
        "model_fallbacks:\n"
        "  codex:\n"
        "    smart: [gpt-smart, gpt-backup]\n"
        "agents:\n"
        "  planner:\n"
        "    runtimes: [claude_code, codex]\n"
        "    model: smart\n",
    )

    role = load_project_config(tmp_path).resolve_role("planner")

    assert [provider.runtime for provider in role.providers] == ["claude_code", "codex"]
    assert [provider.model for provider in role.providers] == ["fable", "gpt-smart"]
    assert role.providers[0].fallbacks == ["opus", "sonnet"]
    assert role.providers[1].fallbacks == ["gpt-backup"]
    # The compatibility fields remain the first provider, exactly like scalar runtime.
    assert (role.runtime, role.model, role.fallbacks) == (
        "claude_code",
        "fable",
        ["opus", "sonnet"],
    )


def test_runtime_override_replaces_an_ordered_chain_with_one_provider(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_tiers:\n  codex:\n    smart: gpt-smart\n"
        "agents:\n  planner:\n    runtimes: [claude_code, codex]\n",
    )

    role = load_project_config(tmp_path).resolve_role("planner", runtime_override="codex")

    assert [provider.runtime for provider in role.providers] == ["codex"]
    assert role.model == "gpt-smart"


def test_role_rejects_ambiguous_scalar_and_ordered_runtime_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "agents:\n  implementer:\n    runtime: claude_code\n    runtimes: [claude_code, codex]\n",
    )

    with pytest.raises(
        pydantic.ValidationError, match="runtime and runtimes are mutually exclusive"
    ):
        load_project_config(tmp_path)


def test_role_rejects_an_empty_or_duplicate_runtime_chain(tmp_path: Path) -> None:
    _write_config(tmp_path, "agents:\n  implementer:\n    runtimes: []\n")
    with pytest.raises(pydantic.ValidationError, match="at least one provider"):
        load_project_config(tmp_path)

    _write_config(
        tmp_path,
        "agents:\n  implementer:\n    runtimes: [claude_code, claude_code]\n",
    )
    with pytest.raises(pydantic.ValidationError, match="must not repeat"):
        load_project_config(tmp_path)


def test_cross_provider_chain_rejects_a_concrete_model_slug(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "agents:\n  reviewer:\n    runtimes: [claude_code, codex]\n    model: claude-opus-5\n",
    )

    with pytest.raises(RuntimeError, match="must use a model tier"):
        load_project_config(tmp_path).resolve_role("reviewer")


def test_execution_and_doctor_share_chain_shape_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        "agents:\n  reviewer:\n    runtimes: [claude_code, codex]\n    model: claude-opus-5\n",
    )
    config = load_project_config(tmp_path)
    calls: list[tuple[str, list[str]]] = []
    real_validate = config_mod._validate_runtime_chain_shape

    def spy(
        role_name: str,
        runtimes: list[str],
        requested: str | None,
        tier_names: set[str],
    ) -> None:
        if len(runtimes) > 1:
            calls.append((role_name, runtimes))
        real_validate(role_name, runtimes, requested, tier_names)

    monkeypatch.setattr(config_mod, "_validate_runtime_chain_shape", spy)

    with pytest.raises(RuntimeChainConfigError) as exc:
        config.resolve_role("reviewer")
    report = {row.name: row for row in role_reports(config)}["reviewer"]

    assert calls == [
        ("reviewer", ["claude_code", "codex"]),
        ("reviewer", ["claude_code", "codex"]),
    ]
    assert report.error == str(exc.value)


def test_missing_tier_for_the_effective_runtime_raises(tmp_path: Path) -> None:
    """Better a clear error here than a foreign model name reaching the CLI."""
    config = load_project_config(tmp_path)  # defaults define claude_code tiers only

    with pytest.raises(ModelTierError) as exc:
        config.resolve_role("reviewer", runtime_override="codex")

    message = str(exc.value)
    assert "codex" in message and "smart" in message


def test_chain_prunes_a_provider_with_a_missing_tier_when_another_resolves(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        "agents:\n  planner:\n    runtimes: [claude_code, codex]\n",
    )
    config = load_project_config(tmp_path)

    role = config.resolve_role("planner")
    report = {row.name: row for row in role_reports(config)}["planner"]

    assert [provider.runtime for provider in role.providers] == ["claude_code"]
    assert role.runtime == "claude_code"
    assert len(role.diagnostics) == 1
    assert "codex" in role.diagnostics[0]
    assert "model_tiers.codex" in role.diagnostics[0]
    assert report.error is None
    assert report.runtime == "claude_code"
    assert [(provider.runtime, provider.error is None) for provider in report.providers] == [
        ("claude_code", True),
        ("codex", False),
    ]


def test_chain_raises_when_no_provider_can_resolve_the_requested_tier(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        "agents:\n  planner:\n    runtimes: [codex, another]\n",
    )

    with pytest.raises(RuntimeChainConfigError, match="no provider with a resolvable model"):
        load_project_config(tmp_path).resolve_role("planner")


def test_concrete_models_are_not_treated_as_tiers(tmp_path: Path) -> None:
    _write_config(tmp_path, "agents:\n  reviewer:\n    runtime: codex\n    model: gpt-5-codex\n")
    config = load_project_config(tmp_path)
    # not a tier name anywhere, so it passes through even without a codex table
    assert config.resolve_role("reviewer").model == "gpt-5-codex"


def test_shipped_defaults_resolve_a_non_empty_ladder_for_every_role(tmp_path: Path) -> None:
    """Regression for #45: defaults.yaml must configure a real ladder, or
    run_with_fallback has nowhere to step down and every role's fallback
    silently becomes a no-op again."""
    config = load_project_config(tmp_path)

    for role in ROLE_NAMES:
        resolved = config.resolve_role(role)
        fallbacks = resolved.fallbacks
        assert fallbacks, f"{role} has no configured fallbacks"
        assert resolved.model not in fallbacks
        assert len(fallbacks) == len(set(fallbacks))

    reports = {r.name: r for r in role_reports(config)}
    for role in ROLE_NAMES:
        assert reports[role].error is None
        assert reports[role].fallbacks


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


def test_model_tier_error_carries_the_runtime_tier_and_role(tmp_path: Path) -> None:
    """Structured fields, so a diagnostic can group gaps instead of re-parsing prose."""
    config = load_project_config(tmp_path)

    with pytest.raises(ModelTierError) as exc:
        config.resolve_role("reviewer", runtime_override="codex")

    assert (exc.value.runtime, exc.value.tier, exc.value.role) == ("codex", "smart", "reviewer")


def test_role_reports_can_answer_for_another_runtime(tmp_path: Path) -> None:
    """The same resolution `--runtime <name>` would do, without running anything."""
    _write_config(tmp_path, "model_tiers:\n  codex:\n    smart: some-codex-model\n")
    config = load_project_config(tmp_path)

    reports = {r.name: r for r in role_reports(config, runtime="codex")}

    assert reports["reviewer"].runtime == "codex"
    assert reports["reviewer"].model == "some-codex-model"
    # `fast` is undefined for codex here, so the implementer is a named gap
    assert reports["implementer"].missing_tier == "fast"


def test_runtime_reports_group_missing_tiers_by_tier(tmp_path: Path) -> None:
    """The shipped defaults define claude_code only, so codex is entirely absent."""
    config = load_project_config(tmp_path)

    reports = {r.runtime: r for r in runtime_reports(config, ["claude_code", "codex"])}

    assert reports["claude_code"].missing_tiers() == {}
    assert reports["codex"].missing_tiers() == {
        "smart": ["planner", "reviewer"],
        "fast": ["implementer"],
    }


def test_runtime_reports_resolve_models_once_the_table_exists(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_tiers:\n  codex:\n    smart: some-codex-model\n    fast: some-small-codex-model\n",
    )
    config = load_project_config(tmp_path)

    (report,) = runtime_reports(config, ["codex"])

    assert report.missing_tiers() == {}
    assert [r.model for r in report.roles] == [
        "some-codex-model",  # planner
        "some-small-codex-model",  # implementer
        "some-codex-model",  # reviewer
    ]


def test_runtime_reports_ignore_a_role_level_runtime(tmp_path: Path) -> None:
    """`--runtime` beats a role's own `runtime:`, so the audit must too."""
    _write_config(tmp_path, "agents:\n  implementer:\n    runtime: codex\n")
    config = load_project_config(tmp_path)

    (report,) = runtime_reports(config, ["claude_code"])

    assert [r.runtime for r in report.roles] == ["claude_code"] * 3
    assert report.missing_tiers() == {}


def test_ladder_warnings_flag_a_tier_that_is_not_on_its_own_ladder(tmp_path: Path) -> None:
    """The step-UP hazard: an untrimmed ladder hands back a model nobody chose."""
    _write_config(
        tmp_path,
        "model_tiers:\n  claude_code:\n    smart: haiku\n"
        "model_fallbacks:\n  claude_code:\n    smart: [fable, opus]\n",
    )

    (warning,) = [w for w in ladder_warnings(load_project_config(tmp_path)) if "smart" in w]

    assert "'haiku'" in warning
    assert "step UP into 'fable'" in warning


def test_ladder_warnings_flag_a_ladder_for_a_tier_the_runtime_does_not_define(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, "model_fallbacks:\n  codex:\n    smart: [a-model, another-model]\n")

    warnings = ladder_warnings(load_project_config(tmp_path))

    assert any("model_fallbacks.codex.smart" in w and "nothing can reach it" in w for w in warnings)


def test_ladder_warnings_stay_quiet_for_an_emptied_ladder(tmp_path: Path) -> None:
    """Clearing a ladder is how a project opts out — not a misconfiguration."""
    _write_config(tmp_path, "model_fallbacks:\n  claude_code:\n    smart: []\n    fast: []\n")

    assert ladder_warnings(load_project_config(tmp_path)) == []


def test_shipped_defaults_have_no_ladder_warnings(tmp_path: Path) -> None:
    assert ladder_warnings(load_project_config(tmp_path)) == []


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


def test_scout_focus_loads_from_the_target_repo(tmp_path: Path) -> None:
    """The focus travels with the repo scout runs against, not the platform."""
    _write_config(tmp_path, 'scout:\n  focus: "Pages with no meta description."\n')
    assert load_project_config(tmp_path).scout.focus == "Pages with no meta description."


def test_scout_focus_defaults_to_empty(tmp_path: Path) -> None:
    assert load_project_config(tmp_path).scout.focus == ""
    _write_config(tmp_path, "base_branch: develop\n")
    assert load_project_config(tmp_path).scout.focus == ""


def test_tui_theme_defaults_to_catppuccin_macchiato(tmp_path: Path) -> None:
    assert load_project_config(tmp_path).tui.theme == "catppuccin-macchiato"


def test_tui_theme_project_override_wins(tmp_path: Path) -> None:
    _write_config(tmp_path, "tui:\n  theme: nord\n")
    assert load_project_config(tmp_path).tui.theme == "nord"


def test_tui_chat_sink_defaults_to_none(tmp_path: Path) -> None:
    """None means the TUI's `c` auto-probes the sink table (#249) instead of
    a fixed transport — the same "unset means detect" shape as `runtime.name`
    elsewhere in this file."""
    assert load_project_config(tmp_path).tui.chat_sink is None
    _write_config(tmp_path, "base_branch: develop\n")
    assert load_project_config(tmp_path).tui.chat_sink is None


def test_tui_chat_sink_loads_a_free_form_command(tmp_path: Path) -> None:
    _write_config(tmp_path, 'tui:\n  chat_sink: "mymux send --pane right -- {text}"\n')
    assert load_project_config(tmp_path).tui.chat_sink == "mymux send --pane right -- {text}"


def test_tui_chat_sink_without_placeholder_fails_at_load(tmp_path: Path) -> None:
    """A template with no `{text}` would reach `CommandSink.send` with nothing
    to substitute — refused here, naming `tui.chat_sink`, rather than a sink
    that silently sends the same literal command on every issue (#249)."""
    _write_config(tmp_path, 'tui:\n  chat_sink: "mymux send --pane right"\n')
    with pytest.raises(pydantic.ValidationError, match="tui.chat_sink"):
        load_project_config(tmp_path)


def test_tui_chat_sink_blank_fails_at_load(tmp_path: Path) -> None:
    """A whitespace-only template would `shlex.split` to `[]`, and `run([])`
    breaks the sink's never-raise contract — refused here instead."""
    _write_config(tmp_path, 'tui:\n  chat_sink: "   "\n')
    with pytest.raises(pydantic.ValidationError, match="tui.chat_sink"):
        load_project_config(tmp_path)


def test_tui_chat_sink_with_unbalanced_quotes_fails_at_load(tmp_path: Path) -> None:
    """#249 review finding: the validator must `shlex.split` the template
    itself, not just check for the `{text}` substring — a template
    `shlex.split` chokes on (an unbalanced quote here) would otherwise reach
    `CommandSink.send` and raise there instead of failing at config load."""
    _write_config(tmp_path, "tui:\n  chat_sink: 'mymux send --pane \"right -- {text}'\n")
    with pytest.raises(pydantic.ValidationError, match="tui.chat_sink"):
        load_project_config(tmp_path)


def test_tui_chat_sink_requires_text_as_its_own_token(tmp_path: Path) -> None:
    """`{text}` must be a standalone argv token, not merely a substring of
    one — `--data=foo{text}bar` has no single argv element `send()` could
    substitute the whole payload into, so a plain substring check would let
    a broken template through (#249 review finding)."""
    _write_config(tmp_path, 'tui:\n  chat_sink: "mymux --data=foo{text}bar"\n')
    with pytest.raises(pydantic.ValidationError, match="tui.chat_sink"):
        load_project_config(tmp_path)

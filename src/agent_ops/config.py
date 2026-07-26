from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT

DEFAULTS_FILE = PLATFORM_ROOT / "config" / "defaults.yaml"
PROJECT_CONFIG_REL = Path(".agent") / "config.yaml"

ROLE_NAMES = ("planner", "implementer", "reviewer")


class ModelTierError(RuntimeError):
    """A role asked for a tier the effective runtime does not define.

    RuntimeError so the CLI's existing error handling reports it as a clean
    message; the alternative is handing a foreign model name to a CLI and
    letting it 400 halfway through a run.
    """


class Commands(BaseModel):
    setup: str | None = None  # run once in each fresh worktree before gates (e.g. npm install)
    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class LoopConfig(BaseModel):
    max_attempts: int = 3
    gates: list[str] = Field(default_factory=lambda: ["test", "lint", "typecheck"])
    # Wall-clock bound per gate command (and per `commands.setup` run). How long
    # a test suite takes is per-project, hence a knob; the default sits well
    # under the CI lane's 55-minute job timeout so a wedged gate still leaves
    # room for the run to report.
    gate_timeout_seconds: float = 1800.0
    plan: bool = True
    self_review: bool = True
    auto_merge: bool = False  # after opening a PR, merge it into staging if merge rules pass


class RuntimeConfig(BaseModel):
    name: str = "claude_code"
    model: str | None = None
    permission_mode: str = "acceptEdits"
    # The mode for `agent spawn`'s interactive sessions, kept separate from the
    # headless one above because the two paths fail in opposite directions. A
    # headless run has nobody to ask, so an unapproved tool is *denied* and the
    # run carries on; an interactive one *waits*, and a delegated worker that
    # waits looks dead — its stop hook fires and reports it halted (issue #115).
    # Loosen or tighten per project here, or per spawn with `--permission-mode`.
    interactive_permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    stream: bool = True


class RoleConfig(BaseModel):
    """Per-role overrides; unset fields fall back to the project's runtime config."""

    runtime: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    max_turns: int | None = None


class AgentsConfig(BaseModel):
    planner: RoleConfig = Field(default_factory=RoleConfig)
    implementer: RoleConfig = Field(default_factory=RoleConfig)
    reviewer: RoleConfig = Field(default_factory=RoleConfig)


class ResolvedRole(BaseModel):
    runtime: str
    model: str | None
    permission_mode: str
    max_turns: int | None
    # Models to try, in order, only if `model` turns out to be unavailable.
    # Empty unless the project configures a ladder — no ladder, no change.
    fallbacks: list[str] = Field(default_factory=list)


class MergeConfig(BaseModel):
    """Rules for agent merges into the working branch (never the stable one)."""

    stable_branch: str = "main"  # promotion target; only humans merge into it
    max_changed_lines: int = 400
    max_changed_files: int = 12
    blocked_paths: list[str] = Field(
        default_factory=lambda: [
            ".github/*",
            "*auth*",
            "*migration*",
            "package.json",
            "package-lock.json",
            "requirements*.txt",
            "pyproject.toml",
            "uv.lock",
            "Dockerfile",
            "*.tf",
        ]
    )


class ReviewConfig(BaseModel):
    """Budget for the diff inlined into the reviewer prompt."""

    max_diff_lines: int = 5000


class ProjectConfig(BaseModel):
    base_branch: str = "main"
    worktree_dir: str = ".worktrees"
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    # Per-runtime tier names → concrete models, e.g.
    # {"claude_code": {"smart": "fable", "fast": "sonnet"}}. Roles reference
    # tiers ("smart") so upgrading every role is a one-line change, and
    # floating vendor aliases keep tiers pointing at the latest models.
    model_tiers: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Per-runtime fallback ladders, keyed by the same names as model_tiers:
    # {"claude_code": {"smart": ["fable", "opus"]}}. Used ONLY when a model
    # turns out to be unavailable mid-run (spend limit, unsupported, retired);
    # a run that never hits a limit never looks at this. Empty by default, so
    # the mechanism is inert until a project (or defaults.yaml) opts in.
    # See docs/workflow.md for how to refresh a ladder against the Models API.
    model_fallbacks: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    commands: Commands = Field(default_factory=Commands)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    skills: list[str] = Field(default_factory=list)

    def effective_runtime(self, role_name: str, runtime_override: str | None = None) -> str:
        """The runtime a role will actually run on: CLI override, role, then base."""
        role: RoleConfig = getattr(self.agents, role_name)
        return runtime_override or role.runtime or self.runtime.name

    def resolve_role(self, role_name: str, *, runtime_override: str | None = None) -> ResolvedRole:
        """Merge a role's overrides over the base runtime config, mapping model tiers.

        Tiers resolve against the *effective* runtime, so `--runtime codex`
        looks up `smart` in the codex table rather than handing codex a Claude
        model name.
        """
        role: RoleConfig = getattr(self.agents, role_name)
        runtime = self.effective_runtime(role_name, runtime_override)
        tiers = self.model_tiers.get(runtime, {})
        requested = role.model or self.runtime.model

        model = requested
        if requested is not None:
            if requested in tiers:
                model = tiers[requested]
            elif requested in self.tier_names():
                # A tier name every other runtime knows, missing here — passing
                # it through would just fail later inside the CLI.
                defined = ", ".join(sorted(tiers)) or "no tiers at all"
                raise ModelTierError(
                    f"runtime {runtime!r} has no model for tier {requested!r} "
                    f"(role {role_name!r}); model_tiers.{runtime} defines {defined}. "
                    f"Add the tier to model_tiers.{runtime} or pin a concrete model on the role."
                )

        return ResolvedRole(
            runtime=runtime,
            model=model,
            permission_mode=role.permission_mode or self.runtime.permission_mode,
            max_turns=role.max_turns if role.max_turns is not None else self.runtime.max_turns,
            fallbacks=self._fallbacks_for(runtime, requested, model),
        )

    def tier_names(self) -> set[str]:
        """Every name used as a tier by any runtime.

        A name only counts as a tier if some runtime declares it; anything else
        is a concrete model the user pinned and is passed through untouched.
        """
        return {tier for tiers in self.model_tiers.values() for tier in tiers}

    def _fallbacks_for(self, runtime: str, requested: str | None, model: str | None) -> list[str]:
        """The rungs below `model` in this runtime's ladder for `requested`.

        The configured ladder is the full sequence (e.g. smart: [fable, opus]),
        so the active model and anything above it are dropped — a ladder can
        only ever step down from where the role already is.
        """
        if requested is None:
            return []
        tiers = self.model_tiers.get(runtime, {})
        rungs = [
            tiers.get(rung, rung)
            for rung in self.model_fallbacks.get(runtime, {}).get(requested, [])
        ]
        if model in rungs:
            rungs = rungs[rungs.index(model) + 1 :]
        return [rung for rung in dict.fromkeys(rungs) if rung != model]


@dataclass(frozen=True)
class RoleReport:
    """What one role would run with — or why it cannot run at all."""

    name: str
    runtime: str
    model: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    error: str | None = None


def role_reports(config: ProjectConfig) -> list[RoleReport]:
    """Resolve every role for `agent doctor`, turning failures into report rows.

    A misconfigured ladder or a tier the runtime does not define should be
    visible before a run dies on it, so resolution errors are reported per role
    instead of aborting the whole check.
    """
    reports: list[RoleReport] = []
    for name in ROLE_NAMES:
        runtime = config.effective_runtime(name)
        try:
            resolved = config.resolve_role(name)
        except ModelTierError as exc:
            reports.append(RoleReport(name=name, runtime=runtime, error=str(exc)))
            continue
        reports.append(
            RoleReport(
                name=name,
                runtime=resolved.runtime,
                model=resolved.model,
                fallbacks=resolved.fallbacks,
            )
        )
    return reports


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_project_config(project_root: Path) -> ProjectConfig:
    """Platform defaults merged with the project's .agent/config.yaml (project wins)."""
    defaults = _load_yaml(DEFAULTS_FILE)
    project = _load_yaml(project_root / PROJECT_CONFIG_REL)
    return ProjectConfig.model_validate(_deep_merge(defaults, project))

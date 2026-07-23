from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT

DEFAULTS_FILE = PLATFORM_ROOT / "config" / "defaults.yaml"
PROJECT_CONFIG_REL = Path(".agent") / "config.yaml"


class Commands(BaseModel):
    setup: str | None = None  # run once in each fresh worktree before gates (e.g. npm install)
    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class LoopConfig(BaseModel):
    max_attempts: int = 3
    gates: list[str] = Field(default_factory=lambda: ["test", "lint", "typecheck"])
    plan: bool = True
    self_review: bool = True


class RuntimeConfig(BaseModel):
    name: str = "claude_code"
    model: str | None = None
    permission_mode: str = "acceptEdits"
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
    commands: Commands = Field(default_factory=Commands)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    skills: list[str] = Field(default_factory=list)

    def resolve_role(self, role_name: str) -> ResolvedRole:
        """Merge a role's overrides over the base runtime config, mapping model tiers."""
        role: RoleConfig = getattr(self.agents, role_name)
        runtime = role.runtime or self.runtime.name
        model = role.model or self.runtime.model
        if model is not None:
            model = self.model_tiers.get(runtime, {}).get(model, model)
        return ResolvedRole(
            runtime=runtime,
            model=model,
            permission_mode=role.permission_mode or self.runtime.permission_mode,
            max_turns=role.max_turns if role.max_turns is not None else self.runtime.max_turns,
        )


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

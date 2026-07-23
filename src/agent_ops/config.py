from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT

DEFAULTS_FILE = PLATFORM_ROOT / "config" / "defaults.yaml"
PROJECT_CONFIG_REL = Path(".agent") / "config.yaml"


class Commands(BaseModel):
    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class LoopConfig(BaseModel):
    max_attempts: int = 3
    gates: list[str] = Field(default_factory=lambda: ["test", "lint", "typecheck"])
    self_review: bool = True


class RuntimeConfig(BaseModel):
    name: str = "claude_code"
    model: str | None = None
    permission_mode: str = "acceptEdits"
    max_turns: int | None = None


class ProjectConfig(BaseModel):
    base_branch: str = "main"
    worktree_dir: str = ".worktrees"
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    commands: Commands = Field(default_factory=Commands)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    skills: list[str] = Field(default_factory=list)


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

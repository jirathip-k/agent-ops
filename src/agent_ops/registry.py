from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT

# Real registry is git-ignored (private repo names stay out of the public
# repo); the committed .example file documents the shape.
REGISTRY_FILE = PLATFORM_ROOT / "config" / "local" / "repos.yml"
EXAMPLE_REGISTRY_FILE = PLATFORM_ROOT / "config" / "repos.example.yml"


class RegistryConfig(BaseModel):
    repos: list[str] = Field(default_factory=list)


def load_registry(path: Path = REGISTRY_FILE) -> RegistryConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy {EXAMPLE_REGISTRY_FILE} there and fill in your repos"
        )
    return RegistryConfig.model_validate(yaml.safe_load(path.read_text()))

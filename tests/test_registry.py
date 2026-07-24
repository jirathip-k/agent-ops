from pathlib import Path

import pytest

from agent_ops.registry import EXAMPLE_REGISTRY_FILE, RegistryConfig, load_registry


def test_example_registry_is_valid() -> None:
    config = load_registry(EXAMPLE_REGISTRY_FILE)
    assert isinstance(config, RegistryConfig)
    assert config.repos


def test_missing_registry_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "repos.yml")

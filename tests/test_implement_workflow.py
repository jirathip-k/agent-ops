from agent_ops.config import ProjectConfig
from agent_ops.workflows.implement import gate_allowed_tools


def test_gate_allowed_tools_covers_each_command() -> None:
    config = ProjectConfig.model_validate(
        {
            "commands": {
                "setup": "npm install",
                "test": "uv run pytest -q",
                "typecheck": "uv run pyright",
            }
        }
    )
    patterns = gate_allowed_tools(config)
    assert "Bash(npm install)" in patterns
    assert "Bash(uv run pytest -q)" in patterns
    assert "Bash(uv run pytest -q:*)" in patterns
    assert "Bash(uv run pyright)" in patterns


def test_gate_allowed_tools_splits_compound_commands() -> None:
    config = ProjectConfig.model_validate(
        {"commands": {"lint": "uv run ruff check . && uv run ruff format --check ."}}
    )
    patterns = gate_allowed_tools(config)
    assert "Bash(uv run ruff check .)" in patterns
    assert "Bash(uv run ruff format --check .)" in patterns


def test_gate_allowed_tools_empty_when_no_commands() -> None:
    assert gate_allowed_tools(ProjectConfig()) == ()

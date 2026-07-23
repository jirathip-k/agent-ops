from pathlib import Path

from agent_ops.board import EXAMPLE_BOARD_FILE, BoardConfig, load_board_config


def test_example_board_config_is_valid() -> None:
    config = load_board_config(EXAMPLE_BOARD_FILE)
    assert config.project.number > 0
    assert config.repos


def test_board_config_parses_label_filter(tmp_path: Path) -> None:
    path = tmp_path / "board.yml"
    path.write_text(
        "project:\n  owner: someuser\n  number: 7\nlabel: agent-ready\nrepos:\n  - a/b\n"
    )
    config = load_board_config(path)
    assert config.project.owner == "someuser"
    assert config.label == "agent-ready"
    assert config.repos == ["a/b"]
    assert isinstance(config, BoardConfig)

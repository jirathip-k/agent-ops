from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT, run

BOARD_FILE = PLATFORM_ROOT / "config" / "board.yml"


class BoardProject(BaseModel):
    owner: str = "@me"
    number: int


class BoardConfig(BaseModel):
    project: BoardProject
    label: str | None = None
    repos: list[str] = Field(default_factory=list)


def load_board_config(path: Path = BOARD_FILE) -> BoardConfig:
    return BoardConfig.model_validate(yaml.safe_load(path.read_text()))


def sync(config: BoardConfig, log: Callable[[str], None] = print) -> int:
    """Add each registered repo's open issues to the Projects board.

    `gh project item-add` is idempotent, so re-running never duplicates items.
    Returns the number of issues processed.
    """
    total = 0
    for repo in config.repos:
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "300",
            "--json",
            "url",
        ]
        if config.label:
            cmd += ["--label", config.label]
        issues = json.loads(run(cmd).stdout)
        for issue in issues:
            run(
                [
                    "gh",
                    "project",
                    "item-add",
                    str(config.project.number),
                    "--owner",
                    config.project.owner,
                    "--url",
                    issue["url"],
                ]
            )
        total += len(issues)
        log(f"{repo}: {len(issues)} issue(s) on board")
    return total

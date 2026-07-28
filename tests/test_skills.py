from __future__ import annotations

from pathlib import Path

import pytest

from agent_ops import skills
from agent_ops.skills import load_skills


def _write_platform_skill(platform_dir: Path, name: str, text: str) -> None:
    platform_dir.mkdir(parents=True, exist_ok=True)
    (platform_dir / f"{name}.md").write_text(text)


def _write_project_skill(project_root: Path, name: str, text: str) -> None:
    project_skills = project_root / ".agent" / "skills"
    project_skills.mkdir(parents=True, exist_ok=True)
    (project_skills / f"{name}.md").write_text(text)


def test_project_skill_overrides_platform_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir, "x", "platform text")
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    project_root = tmp_path / "project"
    _write_project_skill(project_root, "x", "project text")

    result = load_skills(["x"], project_root=project_root)

    assert "project text" in result
    assert "platform text" not in result


def test_platform_skill_used_when_no_project_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir, "x", "platform text")
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    project_root = tmp_path / "project"
    project_root.mkdir()

    result = load_skills(["x"], project_root=project_root)

    assert "platform text" in result


def test_platform_skill_used_when_project_root_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir, "x", "platform text")
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    result = load_skills(["x"], project_root=None)

    assert "platform text" in result


def test_unknown_skill_raises_naming_candidate_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(FileNotFoundError) as exc_info:
        load_skills(["missing"], project_root=project_root)
    message = str(exc_info.value)
    assert "missing" in message
    assert str(project_root / ".agent" / "skills" / "missing.md") in message
    assert str(platform_dir / "missing.md") in message

    with pytest.raises(FileNotFoundError) as exc_info_no_project:
        load_skills(["missing"], project_root=None)
    message_no_project = str(exc_info_no_project.value)
    assert "missing" in message_no_project
    assert str(platform_dir / "missing.md") in message_no_project


def test_skills_concatenate_in_given_order_under_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir, "a", "a-text")
    _write_platform_skill(platform_dir, "b", "b-text")
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    forward = load_skills(["a", "b"], project_root=None)
    assert forward == "## Skill: a\n\na-text\n\n## Skill: b\n\nb-text"

    reverse = load_skills(["b", "a"], project_root=None)
    assert reverse == "## Skill: b\n\nb-text\n\n## Skill: a\n\na-text"


def test_empty_names_returns_empty_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    assert load_skills([], project_root=None) == ""

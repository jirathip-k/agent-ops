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


def test_traversal_name_rejected_even_when_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Secret lives as a sibling of the platform skills dir, at
    # <tmp_path>/secret.md — reachable via "../secret" if traversal worked.
    secret = tmp_path / "secret.md"
    secret.write_text("top secret contents")

    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    with pytest.raises(ValueError, match="../secret"):
        load_skills(["../secret"], project_root=None)


def test_traversal_name_rejected_escaping_project_skills_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Secret lives two levels above project/.agent/skills/, reachable via
    # "../../secret" if traversal worked.
    secret = tmp_path / "secret.md"
    secret.write_text("top secret contents")

    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(ValueError, match="../../secret"):
        load_skills(["../../secret"], project_root=project_root)


@pytest.mark.parametrize("name", ["sub/skill", "sub\\skill"])
def test_separator_names_rejected(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    with pytest.raises(ValueError):
        load_skills([name], project_root=None)


def test_absolute_name_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    with pytest.raises(ValueError):
        load_skills(["/etc/passwd"], project_root=None)


@pytest.mark.parametrize("name", ["", ".", "..", ".hidden"])
def test_degenerate_names_rejected(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    with pytest.raises(ValueError):
        load_skills([name], project_root=None)


def test_invalid_name_error_has_no_filesystem_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    platform_dir.mkdir()
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    with pytest.raises(ValueError) as exc_info:
        load_skills(["../../etc/passwd"], project_root=None)
    message = str(exc_info.value)
    assert "../../etc/passwd" in message
    assert str(platform_dir) not in message


def test_hyphen_and_underscore_names_still_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir, "my-skill", "hyphen text")
    _write_platform_skill(platform_dir, "my_skill", "underscore text")
    monkeypatch.setattr(skills, "PLATFORM_SKILLS", platform_dir)

    result = load_skills(["my-skill", "my_skill"], project_root=None)

    assert "hyphen text" in result
    assert "underscore text" in result

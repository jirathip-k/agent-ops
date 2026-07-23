from __future__ import annotations

from pathlib import Path

from agent_ops.utils import PLATFORM_ROOT

PLATFORM_SKILLS = PLATFORM_ROOT / "skills"


def load_skills(names: list[str], project_root: Path | None = None) -> str:
    """Concatenate skill markdown files for prompt injection.

    Search order: project .agent/skills/ first (project overrides platform),
    then the platform skills/ directory. Unknown names raise — a typo silently
    dropping a skill is worse than an error.
    """
    sections: list[str] = []
    for name in names:
        path = _resolve(name, project_root)
        sections.append(f"## Skill: {name}\n\n{path.read_text().strip()}")
    return "\n\n".join(sections)


def _resolve(name: str, project_root: Path | None) -> Path:
    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(project_root / ".agent" / "skills" / f"{name}.md")
    candidates.append(PLATFORM_SKILLS / f"{name}.md")
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Skill {name!r} not found in: {[str(c) for c in candidates]}")

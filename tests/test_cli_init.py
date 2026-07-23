from pathlib import Path

from typer.testing import CliRunner

from agent_ops.cli import app

runner = CliRunner()


def test_init_scaffolds_project(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0

    assert (tmp_path / ".agent" / "config.yaml").exists()
    assert (tmp_path / ".agent" / "skills").is_dir()
    assert (tmp_path / "AGENTS.md").exists()

    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.is_symlink()
    assert claude_md.readlink() == Path("AGENTS.md")
    assert claude_md.read_text() == (tmp_path / "AGENTS.md").read_text()

    assert ".worktrees/" in (tmp_path / ".gitignore").read_text()


def test_init_is_idempotent_and_keeps_existing_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# my existing instructions\n")
    runner.invoke(app, ["init", "--project", str(tmp_path)])

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0
    # existing CLAUDE.md untouched, not replaced by a symlink
    assert not (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").read_text() == "# my existing instructions\n"
    # .gitignore not appended twice
    assert (tmp_path / ".gitignore").read_text().count(".worktrees/") == 1

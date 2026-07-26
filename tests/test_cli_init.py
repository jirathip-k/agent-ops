import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import github
from agent_ops.cli import app
from agent_ops.cli import github as cli_github

runner = CliRunner()


def _init_repo_with_remote(
    tmp_path: Path, url: str = "https://github.com/acme/widgets.git"
) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", url], cwd=tmp_path, check=True, capture_output=True
    )


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

    gitignore_text = (tmp_path / ".gitignore").read_text()
    assert ".worktrees/" in gitignore_text
    assert ".agent-runs/" in gitignore_text

    template = tmp_path / ".github" / "ISSUE_TEMPLATE" / "task.md"
    assert "Acceptance criteria" in template.read_text()

    # no `origin` remote in a bare tmp_path — labels can't be synced, so
    # onboarding falls back to naming them instead
    assert "no `origin` remote" in result.output
    assert "gh label create spec-requested" in result.output
    assert "gh label create plan-requested" in result.output
    assert "gh label create agent-ready" in result.output


def test_init_print_labels_prints_without_touching_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo_with_remote(tmp_path)

    def fail_sync(project_root: Path, labels: dict, *, repo: str | None = None) -> github.LabelSync:
        raise AssertionError("sync_labels must not run under --print-labels")

    monkeypatch.setattr(cli_github, "sync_labels", fail_sync)

    result = runner.invoke(app, ["init", "--project", str(tmp_path), "--print-labels"])

    assert result.exit_code == 0
    assert "gh label create agent-ready" in result.output
    assert "--description" in result.output


def test_init_syncs_labels_and_reports_what_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo_with_remote(tmp_path)
    seen_repo: list[str | None] = []

    def fake_sync_labels(
        project_root: Path, labels: dict, *, repo: str | None = None
    ) -> github.LabelSync:
        seen_repo.append(repo)
        return github.LabelSync(
            created=["agent-ready"], updated=["blocked"], unchanged=["backlog"], failed=[]
        )

    monkeypatch.setattr(cli_github, "sync_labels", fake_sync_labels)

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "labels created: agent-ready" in result.output
    assert "labels updated: blocked" in result.output
    assert "gh label create" not in result.output
    # `init` pins the repo it already resolved, so a fork's `gh` base-repo
    # resolution can't redirect the sync to the wrong repository.
    assert seen_repo == ["acme/widgets"]


def test_init_reports_a_fully_synced_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo_with_remote(tmp_path)

    def fake_sync_labels(
        project_root: Path, labels: dict, *, repo: str | None = None
    ) -> github.LabelSync:
        return github.LabelSync(created=[], updated=[], unchanged=list(labels), failed=[])

    monkeypatch.setattr(cli_github, "sync_labels", fake_sync_labels)

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "already match" in result.output


def test_init_reports_a_label_sync_failure_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo_with_remote(tmp_path)

    def fake_sync_labels(
        project_root: Path, labels: dict, *, repo: str | None = None
    ) -> github.LabelSync:
        return github.LabelSync(
            created=[], updated=[], unchanged=[], failed=[("blocked", "HTTP 403: no write scope")]
        )

    monkeypatch.setattr(cli_github, "sync_labels", fake_sync_labels)

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "blocked" in result.output
    assert "HTTP 403: no write scope" in result.output


def test_init_falls_back_to_printing_labels_when_gh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `gh` failure (not logged in, network blip, ...) must not leave `init`
    exiting non-zero after scaffolding has already succeeded — see the
    --print-labels and no-`origin` paths just above, which already exit 0."""
    _init_repo_with_remote(tmp_path)

    def failing_sync(project_root: Path, labels: dict, *, repo: str | None = None) -> None:
        raise github.CommandError("gh: not logged in")

    monkeypatch.setattr(cli_github, "sync_labels", failing_sync)

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "could not sync labels" in result.output
    assert "gh label create agent-ready" in result.output


def test_init_falls_back_to_printing_labels_when_gh_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`utils.run` raises FileNotFoundError (an OSError), not CommandError, when
    `gh` isn't on PATH — the ordinary case when onboarding a fresh box. `init`
    must fall back the same way it does for `CommandError`, not crash after
    scaffolding has already succeeded."""
    _init_repo_with_remote(tmp_path)

    def missing_gh(project_root: Path, labels: dict, *, repo: str | None = None) -> None:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(cli_github, "sync_labels", missing_gh)

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "could not sync labels" in result.output
    assert "gh label create agent-ready" in result.output


def test_init_in_a_subdirectory_of_a_checkout_does_not_sync_the_enclosing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git discovers a repo by walking UP from cwd, so `agent init --project
    ./newthing` for a plain directory inside an existing checkout must not
    resolve `remote_slug` to the ENCLOSING repo and sync labels there — an
    unrequested write to a repo nobody named."""
    _init_repo_with_remote(tmp_path)
    nested = tmp_path / "newthing"
    nested.mkdir()

    def fail_sync(project_root: Path, labels: dict, *, repo: str | None = None) -> github.LabelSync:
        raise AssertionError("sync_labels must not run for a dir that is not its own repo root")

    monkeypatch.setattr(cli_github, "sync_labels", fail_sync)

    result = runner.invoke(app, ["init", "--project", str(nested)])

    assert result.exit_code == 0
    assert "not a git repository root" in result.output
    assert "gh label create agent-ready" in result.output


def test_init_is_idempotent_and_keeps_existing_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# my existing instructions\n")
    runner.invoke(app, ["init", "--project", str(tmp_path)])

    result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0
    # existing CLAUDE.md untouched, not replaced by a symlink
    assert not (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").read_text() == "# my existing instructions\n"
    # .gitignore not appended twice
    gitignore_text = (tmp_path / ".gitignore").read_text()
    assert gitignore_text.count(".worktrees/") == 1
    assert gitignore_text.count(".agent-runs/") == 1
    # existing issue template untouched
    template = tmp_path / ".github" / "ISSUE_TEMPLATE" / "task.md"
    template.write_text("custom\n")
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert template.read_text() == "custom\n"

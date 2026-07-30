import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import cli, stubs
from agent_ops.cli import _checkout_drift, _missing_gitignore_markers, app
from agent_ops.utils import run

runner = CliRunner()


def _all_clis_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every CLI `doctor` looks for is installed.

    Patches `shutil.which` itself rather than `agent_ops.cli`'s reference to
    it: the runtime rows answer through each adapter's own `available()`, so a
    patch scoped to the CLI module would leave them reporting whatever happens
    to be on the developer's PATH — passing locally and failing in CI.
    """
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")


IN_SYNC_TRIAGE_CALLER = """
jobs:
  triage:
    permissions:
      contents: write
      issues: write
      pull-requests: write
      id-token: write
      checks: read
      statuses: read
      actions: read
    uses: acme/agent-ops/.github/workflows/triage-pipeline.yml@main
    with:
      target_repo: ${{ github.repository }}
      max_issues: 3
      auto_merge: false
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}
      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}
"""


IN_SYNC_GROOM_CALLER = """
jobs:
  groom:
    permissions:
      contents: read
      issues: write
    uses: acme/agent-ops/.github/workflows/groom-pipeline.yml@main
    with:
      target_repo: ${{ github.repository }}
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
"""


def _write_caller(root: Path, name: str, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(text)


def _write_triage_caller(root: Path, text: str) -> None:
    _write_caller(root, "triage.yml", text)


def _fake_stubs(root: Path, **lanes: str) -> Path:
    """A stand-in stubs/ directory: one `managed-repo-<lane>.yml` per keyword."""
    stubs_dir = root / "stubs"
    stubs_dir.mkdir(exist_ok=True)
    for lane, text in lanes.items():
        (stubs_dir / f"managed-repo-{lane}.yml").write_text(text)
    return stubs_dir


def _commit(repo: Path, message: str) -> None:
    (repo / f"{message}.txt").write_text(f"{message}\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)


@pytest.fixture()
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run(["git", "init", "-b", "main", "--bare"], cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    run(["git", "init", "-b", "main"], cwd=seed)
    run(["git", "config", "user.email", "test@example.com"], cwd=seed)
    run(["git", "config", "user.name", "test"], cwd=seed)
    _commit(seed, "init")
    run(["git", "remote", "add", "origin", str(origin)], cwd=seed)
    run(["git", "push", "-u", "origin", "main"], cwd=seed)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)])
    run(["git", "config", "user.email", "test@example.com"], cwd=clone)
    run(["git", "config", "user.name", "test"], cwd=clone)
    return origin, clone


def _push_from_seed(tmp_path: Path, origin: Path) -> None:
    seed = tmp_path / "seed"
    _commit(seed, "upstream-change")
    run(["git", "push", "origin", "main"], cwd=seed)


def test_missing_gitignore_markers_both_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".worktrees/\n.agent-runs/\n")
    assert _missing_gitignore_markers(tmp_path) == []


def test_missing_gitignore_markers_only_worktrees_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".worktrees/\n")
    assert _missing_gitignore_markers(tmp_path) == [".agent-runs/"]


def test_missing_gitignore_markers_only_agent_runs_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".agent-runs/\n")
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/"]


def test_missing_gitignore_markers_neither_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("dist/\n")
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/", ".agent-runs/"]


def test_missing_gitignore_markers_no_gitignore_file(tmp_path: Path) -> None:
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/", ".agent-runs/"]


def test_doctor_warns_on_missing_marker_but_exit_code_stays_zero(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    (tmp_path / ".gitignore").write_text(".worktrees/\n")
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! .gitignore missing .agent-runs/ — run: agent init" in result.output


def test_doctor_no_warning_when_both_markers_present(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert ".gitignore missing" not in result.output


def test_doctor_warns_on_no_gitignore_file(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    (tmp_path / ".gitignore").unlink()
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! .gitignore missing .worktrees/, .agent-runs/ — run: agent init" in result.output


def test_doctor_no_triage_output_when_caller_in_sync(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_warns_on_missing_secrets(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace(
        "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
        "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
        "",
    )
    _write_triage_caller(tmp_path, caller)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "! .github/workflows/triage.yml is behind stubs/managed-repo-triage.yml — "
        "missing secrets: AGENT_APP_ID, AGENT_APP_PRIVATE_KEY" in result.output
    )


def test_doctor_warns_on_missing_permission(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("      actions: read\n", "")
    _write_triage_caller(tmp_path, caller)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "missing permissions: actions" in result.output


def test_doctor_no_warning_for_customised_triage_values(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("max_issues: 3", "max_issues: 10").replace(
        "auto_merge: false", "auto_merge: true\n      runner: blacksmith-2vcpu-ubuntu-2404"
    )
    _write_triage_caller(tmp_path, caller)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_no_output_when_no_triage_yml(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_reports_an_unreadable_stub_without_traceback(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER)
    _all_clis_present(monkeypatch)
    monkeypatch.setattr(stubs, "STUBS_DIR", _fake_stubs(tmp_path, triage="jobs: [not a mapping"))

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! triage drift check skipped:" in result.output


def test_doctor_drift_message_follows_a_patched_stubs_dir(tmp_path: Path, monkeypatch) -> None:
    """The stub path in the warning must come from stubs.STUBS_DIR at call time.

    A by-value import would silently keep reporting the real stub, and an
    unguarded relative_to() would raise for a stub outside PLATFORM_ROOT.
    """
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("      actions: read\n", "")
    _write_triage_caller(tmp_path, caller)

    alt_stubs = _fake_stubs(tmp_path, triage=IN_SYNC_TRIAGE_CALLER)
    monkeypatch.setattr(stubs, "STUBS_DIR", alt_stubs)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert str(alt_stubs / "managed-repo-triage.yml") in result.output
    assert "missing permissions: actions" in result.output


def test_doctor_warns_on_a_drifting_groom_caller(tmp_path: Path, monkeypatch) -> None:
    """#90: every lane the repo calls is checked, not just triage."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_GROOM_CALLER.replace(
        "      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n", ""
    )
    _write_caller(tmp_path, "groom.yml", caller)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "! .github/workflows/groom.yml is behind stubs/managed-repo-groom.yml — "
        "missing secrets: CLAUDE_CODE_OAUTH_TOKEN" in result.output
    )


def test_doctor_reports_each_drifting_lane_on_its_own_line(tmp_path: Path, monkeypatch) -> None:
    """Two drifting lanes must stay two readable lines, not one merged message."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER.replace("      actions: read\n", ""))
    _write_caller(tmp_path, "groom.yml", IN_SYNC_GROOM_CALLER.replace("      issues: write\n", ""))
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! CI lane callers checked: groom, triage" in result.output
    assert "groom.yml is behind stubs/managed-repo-groom.yml — missing permissions: issues" in (
        result.output
    )
    assert "triage.yml is behind stubs/managed-repo-triage.yml — missing permissions: actions" in (
        result.output
    )


def test_doctor_survives_an_unreadable_workflows_directory(tmp_path: Path, monkeypatch) -> None:
    """#242: an unlistable `.github/workflows` must not crash `doctor`.

    `chmod 000` is a no-op for root/CI containers, so `Path.iterdir` is
    patched to raise for this repo's workflows dir specifically instead.
    """
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "triage.yml").write_text(IN_SYNC_TRIAGE_CALLER)
    _all_clis_present(monkeypatch)

    real_iterdir = Path.iterdir

    def fake_iterdir(self: Path) -> object:
        if self == workflows:
            raise PermissionError(13, "Permission denied", str(workflows))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exception is None
    assert result.exit_code == 0
    assert "! CI caller drift check skipped: can't read" in result.output
    assert str(workflows) in result.output


# --- model tiers, per runtime (#42) -----------------------------------------


def _write_project_config(root: Path, body: str) -> None:
    (root / ".agent").mkdir(exist_ok=True)
    (root / ".agent" / "config.yaml").write_text(body)


def test_doctor_lists_the_resolved_model_and_ladder_for_every_role(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "  planner: claude_code / fable (fallbacks: opus → sonnet)" in result.output
    assert "  implementer: claude_code / sonnet (fallbacks: haiku)" in result.output


def test_doctor_warns_that_a_runtime_without_a_tier_table_would_be_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """#42: the refusal is correct, but useless if the only way to find it is to
    run with the override — and people reach for `--runtime` exactly when the
    usual runtime has already stopped working."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert (
        "! --runtime codex would be refused — model_tiers.codex has "
        "no 'fast' for implementer; no 'smart' for planner, reviewer" in result.output
    )
    # a runtime this project never reaches for is not a fault
    assert result.exit_code == 0


def test_doctor_lists_an_alternate_runtimes_models_once_it_has_a_table(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_project_config(
        tmp_path,
        "model_tiers:\n  codex:\n    smart: some-codex-model\n    fast: some-small-codex-model\n",
    )
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "  --runtime codex: planner some-codex-model, implementer some-small-codex-model, "
        "reviewer some-codex-model" in result.output
    )
    assert "would be refused" not in result.output


def test_doctor_fails_when_the_configured_runtimes_cli_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr(shutil, "which", lambda tool: None if tool == "claude" else f"/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "✗ claude_code (missing; it is this project's runtime.name)" in result.output


def test_doctor_tolerates_a_missing_cli_for_a_runtime_the_project_does_not_use(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr(shutil, "which", lambda tool: None if tool == "codex" else f"/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "- codex (optional)" in result.output


def test_doctor_flags_a_ladder_that_could_step_up_into_a_costlier_model(
    tmp_path: Path, monkeypatch
) -> None:
    """A project that repoints a tier but inherits the ladder gets a ladder that
    is never trimmed — invisible until the day an availability failure uses it."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_project_config(tmp_path, "model_tiers:\n  claude_code:\n    smart: haiku\n")
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! model_tiers.claude_code.smart is 'haiku'" in result.output
    assert "step UP into 'fable'" in result.output


def test_doctor_stays_quiet_about_lanes_the_repo_has_not_wired(tmp_path: Path, monkeypatch) -> None:
    """Absent lanes are named once as "not wired" and never warned about."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER)
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "✓ CI lane callers in sync: triage "
        "(not wired: classify, distill, evolve, groom, implement, plan, promote, scout, spec)"
    ) in result.output
    assert "is behind" not in result.output


def test_doctor_says_nothing_when_no_lane_is_wired(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "CI lane callers" not in result.output


def _write_commands(root: Path, body: str) -> None:
    (root / ".agent").mkdir(exist_ok=True)
    (root / ".agent" / "config.yaml").write_text(f"commands:\n{body}")


def test_doctor_hints_when_gates_are_configured_but_requires_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_commands(tmp_path, '  test: "npm run check"\n')
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! commands.requires is empty" in result.output


def test_doctor_stays_quiet_about_requires_when_no_gate_has_a_command(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing to guard: a repo with no gate commands cannot fail one on a missing tool."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "commands.requires" not in result.output


def test_doctor_reports_a_missing_required_tool_as_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A run provisions these via `setup`, so `doctor` reports and never fails on them."""
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_commands(
        tmp_path,
        '  setup: "npm ci"\n  test: "npm run check"\n  requires: [sh, no-such-binary-246]\n',
    )
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "✓ requires on PATH: sh" in result.output
    assert "! requires not on PATH here: no-such-binary-246" in result.output
    assert "npm ci" in result.output


def test_doctor_reports_resolved_requires_without_a_warning(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_commands(tmp_path, '  test: "npm run check"\n  requires: [sh]\n')
    _all_clis_present(monkeypatch)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "✓ requires on PATH: sh" in result.output
    assert "not on PATH here" not in result.output


def test_doctor_does_not_crash_when_the_requires_check_blows_up(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_commands(tmp_path, '  test: "npm run check"\n  requires: [sh]\n')
    _all_clis_present(monkeypatch)

    def explode(*args: object, **kwargs: object) -> list[str]:
        raise OSError("no shell here")

    monkeypatch.setattr(cli, "missing_requirements", explode)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! could not check commands.requires (no shell here)" in result.output


def test_checkout_drift_in_sync(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    assert _checkout_drift(clone) is None


def test_checkout_drift_behind(tmp_path: Path, origin_and_clone: tuple[Path, Path]) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "1 commit behind" in drift
    assert "origin/main" in drift
    assert str(clone) in drift
    assert "ahead" not in drift


def test_checkout_drift_ahead(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    _commit(clone, "local-change")

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "1 commit ahead of origin/main" in drift
    assert "behind" not in drift


def test_checkout_drift_diverged(tmp_path: Path, origin_and_clone: tuple[Path, Path]) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)
    _commit(clone, "local-change")

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "diverged" in drift
    assert "1 commit ahead" in drift
    assert "1 commit behind" in drift


def test_checkout_drift_no_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    run(["git", "config", "user.name", "test"], cwd=repo)
    _commit(repo, "init")

    assert _checkout_drift(repo) is None


def test_checkout_drift_detached_head(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    run(["git", "checkout", "--detach"], cwd=clone)

    assert _checkout_drift(clone) is None


def test_checkout_drift_not_a_repo(tmp_path: Path) -> None:
    assert _checkout_drift(tmp_path) is None


def test_checkout_drift_never_fetches(
    tmp_path: Path, origin_and_clone: tuple[Path, Path], monkeypatch
) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)
    _commit(clone, "local-change")

    recorded: list[list[str]] = []
    real_run = run

    def recording_run(cmd, **kwargs):
        recorded.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("agent_ops.cli.run", recording_run)

    _checkout_drift(clone)

    assert recorded, "expected _checkout_drift to invoke git"
    assert not any("fetch" in cmd for cmd in recorded)


def test_doctor_reports_checkout_drift(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)
    monkeypatch.setattr(
        "agent_ops.cli._checkout_drift",
        lambda root: "agent-ops checkout is 4 commits behind origin/main",
    )

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! agent-ops checkout is 4 commits behind origin/main" in result.output


def test_doctor_silent_when_checkout_in_sync(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _all_clis_present(monkeypatch)
    monkeypatch.setattr("agent_ops.cli._checkout_drift", lambda root: None)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "agent-ops checkout" not in result.output

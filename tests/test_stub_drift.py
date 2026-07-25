from pathlib import Path

from agent_ops import stubs
from agent_ops.stubs import TriageDrift, triage_caller_drift

IN_SYNC_CALLER = """
name: Hourly Agent Triage
on:
  schedule:
    - cron: '0 */4 * * *'
  workflow_dispatch: {}
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


def _fields(drift: TriageDrift | None) -> tuple[list[str], list[str]]:
    """secrets/permissions of a drift that parsed cleanly — path varies per test."""
    assert drift is not None
    assert drift.error is None
    return drift.secrets, drift.permissions


def _write_caller(root: Path, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "triage.yml").write_text(text)


def test_in_sync_caller_has_no_drift(tmp_path: Path) -> None:
    _write_caller(tmp_path, IN_SYNC_CALLER)
    assert _fields(triage_caller_drift(tmp_path)) == ([], [])


def test_missing_secret_keys_are_reported(tmp_path: Path) -> None:
    caller = IN_SYNC_CALLER.replace(
        "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
        "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
        "",
    )
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert _fields(drift) == (["AGENT_APP_ID", "AGENT_APP_PRIVATE_KEY"], [])


def test_missing_permission_key_is_reported(tmp_path: Path) -> None:
    caller = IN_SYNC_CALLER.replace("      actions: read\n", "")
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert _fields(drift) == ([], ["actions"])


def test_customised_values_never_warn(tmp_path: Path) -> None:
    caller = """
name: Hourly Agent Triage
on:
  schedule:
    - cron: '0 * * * *'
  workflow_dispatch: {}
jobs:
  run-pipeline:
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
      max_issues: 10
      auto_merge: true
      runner: blacksmith-2vcpu-ubuntu-2404
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}
      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}
"""
    _write_caller(tmp_path, caller)

    assert _fields(triage_caller_drift(tmp_path)) == ([], [])


def test_no_triage_yml_returns_none(tmp_path: Path) -> None:
    assert triage_caller_drift(tmp_path) is None


def test_missing_stub_reports_error_not_traceback(tmp_path: Path, monkeypatch) -> None:
    _write_caller(tmp_path, IN_SYNC_CALLER)
    monkeypatch.setattr(stubs, "TRIAGE_STUB", tmp_path / "nonexistent-stub.yml")

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.error is not None
    # A missing file must not be reported as a parse failure — that sends you
    # inspecting contents that aren't there.
    assert "can't read" in drift.error
    assert "can't parse" not in drift.error
    assert drift.secrets == []
    assert drift.permissions == []


def test_unparseable_stub_reports_error_not_traceback(tmp_path: Path, monkeypatch) -> None:
    _write_caller(tmp_path, IN_SYNC_CALLER)
    bad_stub = tmp_path / "bad-stub.yml"
    bad_stub.write_text("jobs: [this is not a mapping")
    monkeypatch.setattr(stubs, "TRIAGE_STUB", bad_stub)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.error is not None
    assert "can't parse" in drift.error


def test_secrets_inherit_satisfies_every_key(tmp_path: Path) -> None:
    caller = IN_SYNC_CALLER.replace(
        "    secrets:\n"
        "      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n"
        "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
        "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
        "    secrets: inherit\n",
    )
    _write_caller(tmp_path, caller)

    assert _fields(triage_caller_drift(tmp_path)) == ([], [])


def test_read_all_permissions_does_not_pass_as_satisfied(tmp_path: Path) -> None:
    """`permissions: read-all` grants no write scope, so the stub's writes are still missing.

    Treating any non-mapping value as "can't compare, assume fine" made the
    check silently blind to a caller that had downgraded to read-only.
    """
    caller = IN_SYNC_CALLER.replace(
        "    permissions:\n"
        "      contents: write\n"
        "      issues: write\n"
        "      pull-requests: write\n"
        "      id-token: write\n"
        "      checks: read\n"
        "      statuses: read\n"
        "      actions: read\n",
        "    permissions: read-all\n",
    )
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.error is None
    assert "contents" in drift.permissions
    assert drift.secrets == []


def test_write_all_permissions_satisfies_every_key(tmp_path: Path) -> None:
    caller = IN_SYNC_CALLER.replace(
        "    permissions:\n"
        "      contents: write\n"
        "      issues: write\n"
        "      pull-requests: write\n"
        "      id-token: write\n"
        "      checks: read\n"
        "      statuses: read\n"
        "      actions: read\n",
        "    permissions: write-all\n",
    )
    _write_caller(tmp_path, caller)

    assert _fields(triage_caller_drift(tmp_path)) == ([], [])


def test_unrelated_triage_workflow_is_not_a_caller(tmp_path: Path) -> None:
    """ "triage" is a common workflow name — a stale-bot or labeler isn't a pipeline caller."""
    _write_caller(
        tmp_path,
        """
name: Triage stale issues
on:
  schedule:
    - cron: '0 0 * * *'
jobs:
  stale:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/stale@v9
""",
    )

    assert triage_caller_drift(tmp_path) is None


def test_a_second_job_cannot_mask_the_caller_job_gap(tmp_path: Path) -> None:
    """Keys were unioned across every job, so an unrelated job's grants hid real drift."""
    caller = (
        IN_SYNC_CALLER.replace(
            "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
            "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
            "",
        )
        + """
  notify:
    runs-on: ubuntu-latest
    secrets: inherit
    steps:
      - run: echo done
"""
    )
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.secrets == ["AGENT_APP_ID", "AGENT_APP_PRIVATE_KEY"]


def test_workflow_level_permissions_are_honoured(tmp_path: Path) -> None:
    """GitHub applies root-level `permissions:` to jobs that declare none.

    Reading only job-level values made a correctly-configured caller look like
    it granted nothing — doctor reported all seven permissions missing and told
    the user to duplicate a block they already had.
    """
    caller = """
name: Hourly Agent Triage
on:
  workflow_dispatch: {}
permissions:
  contents: write
  issues: write
  pull-requests: write
  id-token: write
  checks: read
  statuses: read
  actions: read
jobs:
  triage:
    uses: acme/agent-ops/.github/workflows/triage-pipeline.yml@main
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}
      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}
"""
    _write_caller(tmp_path, caller)

    assert _fields(triage_caller_drift(tmp_path)) == ([], [])


def test_job_permissions_replace_workflow_permissions(tmp_path: Path) -> None:
    """Job-level `permissions:` replaces the workflow-level block, never merges with it."""
    caller = """
name: Hourly Agent Triage
on:
  workflow_dispatch: {}
permissions:
  contents: write
  issues: write
  pull-requests: write
  id-token: write
  checks: read
  statuses: read
  actions: read
jobs:
  triage:
    permissions:
      contents: write
    uses: acme/agent-ops/.github/workflows/triage-pipeline.yml@main
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}
      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}
"""
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert "issues" in drift.permissions
    assert "contents" not in drift.permissions


def test_two_caller_jobs_do_not_cover_for_each_other(tmp_path: Path) -> None:
    """A second call to the pipeline must not mask the first's missing secret."""
    caller = (
        IN_SYNC_CALLER
        + """
  triage-manual:
    permissions:
      contents: write
      issues: write
      pull-requests: write
      id-token: write
      checks: read
      statuses: read
      actions: read
    uses: acme/agent-ops/.github/workflows/triage-pipeline.yml@main
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
"""
    )
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.secrets == ["AGENT_APP_ID", "AGENT_APP_PRIVATE_KEY"]


def _write_named(root: Path, name: str, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(text)


def test_caller_is_found_under_any_filename(tmp_path: Path) -> None:
    """F1: callers are detected by their `uses:`, not by being named triage.yml.

    status.detect_lanes is content-based for this reason — a repo may call the
    pipeline from agent-triage.yml or fold it into a larger workflow, and a
    filename-only check is silently blind to both.
    """
    caller = IN_SYNC_CALLER.replace("      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n", "")
    _write_named(tmp_path, "agent-triage.yaml", caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.secrets == ["AGENT_APP_ID"]
    assert drift.path == Path(".github/workflows/agent-triage.yaml")


def test_non_dict_section_is_not_treated_as_satisfied(tmp_path: Path) -> None:
    """F3: a list-valued section declares no keys, so it must not read as a blanket grant."""
    caller = IN_SYNC_CALLER.replace(
        "    secrets:\n"
        "      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n"
        "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
        "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
        "    secrets:\n      - CLAUDE_CODE_OAUTH_TOKEN\n",
    )
    _write_caller(tmp_path, caller)

    drift = triage_caller_drift(tmp_path)

    assert drift is not None
    assert drift.secrets == ["CLAUDE_CODE_OAUTH_TOKEN", "AGENT_APP_ID", "AGENT_APP_PRIVATE_KEY"]


def test_a_pipeline_reference_in_a_comment_is_not_a_caller(tmp_path: Path) -> None:
    """The text prefilter can match a comment; the YAML parse is the authority."""
    _write_caller(
        tmp_path,
        """
name: Not really a caller
on:
  workflow_dispatch: {}
# see acme/agent-ops/.github/workflows/triage-pipeline.yml@main for the real one
jobs:
  noop:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
""",
    )

    assert triage_caller_drift(tmp_path) is None

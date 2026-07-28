from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from agent_ops import grants
from agent_ops.utils import CommandError


def _write_grant(path: Path, **overrides: object) -> Path:
    payload = {
        "issue": 200,
        "granted_by": "jirathip-k",
        "scope": "the two --description string literals and nothing else",
        "paths": ["src/agent_ops/cli.py"],
    }
    payload.update(overrides)
    path.write_text(yaml.safe_dump(payload))
    return path


def test_load_parses_a_valid_grant(tmp_path: Path) -> None:
    path = _write_grant(tmp_path / "grant.yaml")

    grant = grants.load(path, issue=200)

    assert grant.issue == 200
    assert grant.granted_by == "jirathip-k"
    assert grant.paths == ["src/agent_ops/cli.py"]
    assert grant.expires is None


def test_load_refuses_a_mismatched_issue_number(tmp_path: Path) -> None:
    path = _write_grant(tmp_path / "grant.yaml", issue=201)

    with pytest.raises(CommandError, match="#201, not #200"):
        grants.load(path, issue=200)


def test_load_refuses_an_expired_grant(tmp_path: Path) -> None:
    yesterday = date.today() - timedelta(days=1)
    path = _write_grant(tmp_path / "grant.yaml", expires=yesterday.isoformat())

    with pytest.raises(CommandError, match="expired"):
        grants.load(path, issue=200)


def test_load_accepts_a_grant_expiring_in_the_future(tmp_path: Path) -> None:
    tomorrow = date.today() + timedelta(days=1)
    path = _write_grant(tmp_path / "grant.yaml", expires=tomorrow.isoformat())

    grant = grants.load(path, issue=200)

    assert grant.expires == tomorrow


def test_load_fails_on_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "grant.yaml"
    path.write_text(yaml.safe_dump({"issue": 200}))

    with pytest.raises(CommandError, match="invalid"):
        grants.load(path, issue=200)


def test_load_fails_on_a_non_mapping_yaml_document(tmp_path: Path) -> None:
    path = tmp_path / "grant.yaml"
    path.write_text("- just\n- a\n- list\n")

    with pytest.raises(CommandError, match="mapping"):
        grants.load(path, issue=200)


def test_load_fails_clearly_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="could not read"):
        grants.load(tmp_path / "nope.yaml", issue=200)


def test_persist_then_load_persisted_round_trips(tmp_path: Path) -> None:
    grant = grants.Grant(
        issue=7, granted_by="jirathip-k", scope="test scope", paths=["pyproject.toml"]
    )

    grants.persist(tmp_path, grant)
    loaded = grants.load_persisted(tmp_path, 7)

    assert loaded == grant


def test_load_persisted_returns_none_when_nothing_was_ever_persisted(tmp_path: Path) -> None:
    assert grants.load_persisted(tmp_path, 7) is None


def test_load_persisted_still_enforces_expiry(tmp_path: Path) -> None:
    """A grant that expires between cycles must not keep silently applying."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    grant_path = grants.persist_path(tmp_path, 7)
    grant_path.parent.mkdir(parents=True)
    _write_grant(grant_path, issue=7, expires=yesterday)

    with pytest.raises(CommandError, match="expired"):
        grants.load_persisted(tmp_path, 7)


BLOCKED_PATHS = [".github/*", "*auth*", "pyproject.toml"]


def test_scope_violations_passes_a_change_covered_by_the_grant() -> None:
    grant = grants.Grant(issue=1, granted_by="x", scope="s", paths=["pyproject.toml"])

    assert grants.scope_violations(["pyproject.toml"], grant, BLOCKED_PATHS) == []


def test_scope_violations_flags_a_blocked_change_the_grant_does_not_cover() -> None:
    grant = grants.Grant(issue=1, granted_by="x", scope="s", paths=["pyproject.toml"])

    assert grants.scope_violations([".github/workflows/ci.yml"], grant, BLOCKED_PATHS) == [
        ".github/workflows/ci.yml"
    ]


def test_scope_violations_never_flags_a_change_outside_blocked_paths() -> None:
    """A grant only ever narrows an existing restriction — it must not turn an
    ordinary, unblocked change into a violation just because the grant's own
    paths don't happen to mention it."""
    grant = grants.Grant(issue=1, granted_by="x", scope="s", paths=["pyproject.toml"])

    assert grants.scope_violations(["src/agent_ops/cli.py"], grant, BLOCKED_PATHS) == []


def test_scope_violations_matches_case_insensitively_like_merge_py() -> None:
    grant = grants.Grant(issue=1, granted_by="x", scope="s", paths=["*Auth*"])

    assert grants.scope_violations(["src/UseAuth.ts"], grant, BLOCKED_PATHS) == []


def test_scope_violations_reports_every_uncovered_offender() -> None:
    grant = grants.Grant(issue=1, granted_by="x", scope="s", paths=["pyproject.toml"])

    violations = grants.scope_violations(
        [".github/workflows/ci.yml", "pyproject.toml", "src/useAuth.ts"], grant, BLOCKED_PATHS
    )

    assert violations == [".github/workflows/ci.yml", "src/useAuth.ts"]

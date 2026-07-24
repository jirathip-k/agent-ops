from agent_ops.status import bucket_counts, detect_lanes


def _issue(*labels: str) -> dict:
    return {"labels": [{"name": name} for name in labels]}


def test_bucket_counts() -> None:
    issues = [
        _issue("agent-ready"),
        _issue("agent-ready", "type: bug"),
        _issue("needs-human"),
        _issue("backlog"),
        _issue("type: idea"),
        _issue(),
    ]
    assert bucket_counts(issues) == {
        "agent-ready": 2,
        "needs-human": 1,
        "backlog": 1,
        "untriaged": 2,
    }


def test_bucket_counts_empty() -> None:
    assert bucket_counts([]) == {
        "agent-ready": 0,
        "needs-human": 0,
        "backlog": 0,
        "untriaged": 0,
    }


def _stub(lane: str, ref: str = "@main", owner: str = "jirathip-k") -> str:
    return (
        f"name: {lane}\n"
        "jobs:\n"
        f"  {lane}:\n"
        f"    uses: {owner}/agent-ops/.github/workflows/{lane}-pipeline.yml{ref}\n"
    )


def test_detect_lanes_cross_repo_uses() -> None:
    assert detect_lanes({"triage.yml": _stub("triage")}) == {"triage"}


def test_detect_lanes_any_owner() -> None:
    assert detect_lanes({"groom.yml": _stub("groom", owner="someone-else")}) == {"groom"}


def test_detect_lanes_local_uses() -> None:
    content = "jobs:\n  triage:\n    uses: ./.github/workflows/triage-pipeline.yml\n"
    assert detect_lanes({"self.yml": content}) == {"triage"}


def test_detect_lanes_filename_does_not_matter() -> None:
    # Detection is content-based: a renamed stub still counts.
    assert detect_lanes({"nightly-cleanup.yml": _stub("groom")}) == {"groom"}


def test_detect_lanes_multiple_lanes_one_file() -> None:
    content = _stub("triage") + "  groom:\n" + _stub("groom").splitlines()[-1] + "\n"
    assert detect_lanes({"agents.yml": content}) == {"triage", "groom"}


def test_detect_lanes_spec_plan_and_promote() -> None:
    workflows = {
        "spec.yaml": _stub("spec"),
        "plan.yml": _stub("plan", ref="@v2"),
        "promote.yml": _stub("promote"),
    }
    assert detect_lanes(workflows) == {"spec", "plan", "promote"}


def test_detect_lanes_yaml_extension_in_uses() -> None:
    content = "jobs:\n  t:\n    uses: o/agent-ops/.github/workflows/triage-pipeline.yaml@main\n"
    assert detect_lanes({"t.yml": content}) == {"triage"}


def test_detect_lanes_ignores_unrelated_workflows() -> None:
    deploy = (
        "name: deploy\n"
        "jobs:\n"
        "  deploy:\n"
        "    uses: actions/deploy-pages@v4\n"
        "    # mentions triage and groom in a comment only\n"
    )
    assert detect_lanes({"deploy.yml": deploy}) == set()


def test_detect_lanes_empty() -> None:
    assert detect_lanes({}) == set()

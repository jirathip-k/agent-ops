from agent_ops.workflows.triage import parse_triage


def test_parses_result_block() -> None:
    text = """I explored the code. Here are my conclusions.

TRIAGE RESULTS:
#12 agent-ready — clear repro, fix is localized to one component
#13 needs-human — requires a product decision on data retention
#14 backlog — idea without acceptance criteria
"""
    results = parse_triage(text)
    assert [(r.number, r.verdict) for r in results] == [
        (12, "agent-ready"),
        (13, "needs-human"),
        (14, "backlog"),
    ]
    assert results[0].reason.startswith("clear repro")


def test_uses_last_marker_and_ignores_junk_lines() -> None:
    text = (
        "TRIAGE RESULTS:\n#1 backlog — early draft\n"
        "TRIAGE RESULTS:\n#2 agent-ready — final\nnot a result line\n"
    )
    results = parse_triage(text)
    assert [(r.number, r.verdict) for r in results] == [(2, "agent-ready")]


def test_no_marker_returns_empty() -> None:
    assert parse_triage("no block here") == []

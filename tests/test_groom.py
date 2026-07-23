from agent_ops.workflows.groom import parse_groom


def test_parses_all_verdicts() -> None:
    text = """Verified each issue against the working branch.

GROOM RESULTS:
#12 close-fixed — fix present in src/lib/recordingQueue.ts (commit bee4873, PR #120)
#13 close-invalid — duplicate of #12
#14 agent-ready — clear repro; acceptance: chart shows decimal RPE unrounded
#15 needs-human — requires a data-retention decision
#16 backlog — idea without acceptance criteria
#17 keep — already agent-ready and still valid
"""
    results = parse_groom(text)
    assert [(r.number, r.verdict) for r in results] == [
        (12, "close-fixed"),
        (13, "close-invalid"),
        (14, "agent-ready"),
        (15, "needs-human"),
        (16, "backlog"),
        (17, "keep"),
    ]
    assert results[0].reason.startswith("fix present")


def test_uses_last_marker_and_ignores_junk() -> None:
    text = "GROOM RESULTS:\n#1 keep — draft\nGROOM RESULTS:\n#2 close-fixed — final\nnoise\n"
    assert [(r.number, r.verdict) for r in parse_groom(text)] == [(2, "close-fixed")]


def test_no_marker_returns_empty() -> None:
    assert parse_groom("no block here") == []

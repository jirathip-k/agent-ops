from agent_ops.workflows.scout import parse_scout


def test_parses_filed_issues() -> None:
    text = """Mined TODOs and merged-PR threads.

SCOUT RESULTS:
#41 — TODO at src/lib/export.ts:88, silent failure when the sheet is empty
#42 — PR #120 review deferred pagination to a follow-up that was never filed
"""
    results = parse_scout(text)
    assert results is not None
    assert [(r.number, r.reason.split(",")[0]) for r in results] == [
        (41, "TODO at src/lib/export.ts:88"),
        (42, "PR #120 review deferred pagination to a follow-up that was never filed"),
    ]


def test_explicit_none_is_empty_list() -> None:
    assert parse_scout("nothing cleared the bar\n\nSCOUT RESULTS:\nnone\n") == []


def test_no_marker_is_none() -> None:
    assert parse_scout("no block here") is None


def test_uses_last_marker_and_ignores_junk() -> None:
    text = "SCOUT RESULTS:\n#1 — draft\nSCOUT RESULTS:\n#2 — final\nnoise\n"
    results = parse_scout(text)
    assert results is not None
    assert [(r.number, r.reason) for r in results] == [(2, "final")]

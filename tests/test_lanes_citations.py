"""Anti-drift guard for docs/reference/lanes.md's orchestrator.md citations.

lanes.md's own preamble commits to a stronger bar than most docs: "every cell
cites the file and line it was checked against. If a citation has drifted,
that is this page falling out of date -- fix the page, don't route around
it." The review-lane convergence (#171) proved that bar fragile: it shifted
`prompts/orchestrator.md` by inserting the `agent review --check` gate, and
several pre-existing line citations moved out from under it unnoticed until
review caught it by hand. This pins the citations touched by that shift so
the next orchestrator.md edit fails a test instead of a human re-counting
lines.
"""

from __future__ import annotations

from agent_ops.utils import PLATFORM_ROOT

LANES = (PLATFORM_ROOT / "docs" / "reference" / "lanes.md").read_text()
ORCHESTRATOR_LINES = (PLATFORM_ROOT / "prompts" / "orchestrator.md").read_text().splitlines()
EVOLVE_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "evolve-pipeline.yml").read_text().splitlines()
)
CLI_LINES = (PLATFORM_ROOT / "src" / "agent_ops" / "cli.py").read_text().splitlines()
DISTILL_WORKFLOW_LINES = (
    (PLATFORM_ROOT / "src" / "agent_ops" / "workflows" / "distill.py").read_text().splitlines()
)
DISTILL_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "distill-pipeline.yml").read_text().splitlines()
)


def _line(n: int) -> str:
    return ORCHESTRATOR_LINES[n - 1]


def _evolve_pipeline_line(n: int) -> str:
    return EVOLVE_PIPELINE_LINES[n - 1]


def test_merge_shell_out_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:148" in LANES
    assert "agent merge <PR_NUMBER> --project target --check" in _line(148)


def test_auto_merge_range_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:160-171" in LANES
    assert "AUTO_MERGE" in _line(160)
    assert "Any condition failed" in _line(171)


def test_failure_handling_range_citation_matches_orchestrator() -> None:
    assert "lines 105-113" in LANES
    assert "Failure handling:" in _line(105)
    assert "VERDICT: REQUEST CHANGES" in _line(108)
    assert "reviewer feedback for the Implementer to act on." in _line(113)


def test_review_gate_range_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:89-100,108-113,164-165" in LANES
    assert "REVIEW GATE" in _line(89)
    assert "same rule Step 3 applies to a failed" in _line(100)
    assert "Tester verdict PASS and the Step 2A" in _line(164)
    assert "VERDICT: APPROVE" in _line(165)


def test_step2b_reviewer_repair_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:124-125" in LANES
    assert "REVIEWER (prompts/agents/reviewer.md)" in _line(125)


def test_evolve_pipeline_citation_matches_workflow() -> None:
    assert ".github/workflows/evolve-pipeline.yml:160" in LANES
    assert 'uv run agent evolve "$LANE"' in _evolve_pipeline_line(160)


def test_distill_cli_citation_matches_cli() -> None:
    assert "src/agent_ops/cli.py:891" in LANES
    assert "def distill(" in CLI_LINES[891 - 1]


def test_distill_run_citation_matches_workflow() -> None:
    assert "src/agent_ops/workflows/distill.py:168" in LANES
    assert "def run_distill(" in DISTILL_WORKFLOW_LINES[168 - 1]


def test_distill_pipeline_citation_matches_pipeline() -> None:
    assert ".github/workflows/distill-pipeline.yml:132" in LANES
    assert "uv run agent distill" in DISTILL_PIPELINE_LINES[132 - 1]

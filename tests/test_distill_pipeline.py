"""Regression tests for .github/workflows/distill-pipeline.yml (#198 review).

These pin two bugs a self-review caught before this shipped: a missing git
identity (real runners have none configured, unlike a developer's machine)
and a line-count precheck that could disagree with `run_distill`'s own gate
at the `min_lines` boundary.
"""

from __future__ import annotations

import yaml

from agent_ops.utils import PLATFORM_ROOT

_PIPELINE_PATH = PLATFORM_ROOT / ".github" / "workflows" / "distill-pipeline.yml"
_PIPELINE_TEXT = _PIPELINE_PATH.read_text()
_STEPS = yaml.safe_load(_PIPELINE_TEXT)["jobs"]["distill"]["steps"]
_STEP_NAMES = [step.get("name") for step in _STEPS]


def test_git_identity_is_configured_before_running_distill() -> None:
    """GitHub-hosted runners set neither user.name nor user.email. Without this
    step, run_distill's `git commit` dies after the expensive planner session
    has already run — the identity step must exist and precede "Run distill".
    """
    identity_idx = _STEP_NAMES.index("Configure git identity")
    run_idx = _STEP_NAMES.index("Run distill")
    assert identity_idx < run_idx

    identity_step = _STEPS[identity_idx]
    assert "git config --global user.name" in identity_step["run"]
    assert "git config --global user.email" in identity_step["run"]


def test_precheck_line_count_matches_splitlines_at_the_boundary() -> None:
    """run_distill gates on `len(text.splitlines())`, which counts a final line
    even without a trailing newline. `wc -l` counts newlines and undercounts
    such a file by one, so the two could disagree right at min_lines. The
    precheck must use a count that agrees with splitlines(), not wc -l.
    """
    precheck = _STEPS[_STEP_NAMES.index("Check AGENTS.md needs distilling")]
    lines_assignment = next(
        line for line in precheck["run"].splitlines() if line.strip().startswith("LINES=")
    )
    assert "grep -c ''" in lines_assignment
    assert "wc -l" not in lines_assignment

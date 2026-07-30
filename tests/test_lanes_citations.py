"""Anti-drift guard for docs/reference/lanes.md's citations.

lanes.md's own preamble commits to a stronger bar than most docs: every cell
cites where it was checked. That bar used to be enforced with line numbers,
and line numbers turned out fragile: any commit that inserts a line above a
cited symbol invalidates the citation without touching it, and two branches
doing that independently produce a merge conflict where neither side's
number is correct (see #250). Python citations now name a file and a symbol
instead -- `resolve_symbol` re-finds that symbol's definition line by
searching the file, so the citation survives insertions above it and only
breaks when the symbol itself is renamed, moved, deleted, or duplicated,
which is real drift a human needs to see.

Citations into `.github/workflows/*.yml` and `prompts/orchestrator.md` keep
line numbers -- there is no symbol in YAML or prose to anchor to -- and stay
pinned the original way below.
"""

from __future__ import annotations

import re

import pytest

from agent_ops.utils import PLATFORM_ROOT

LANES = (PLATFORM_ROOT / "docs" / "reference" / "lanes.md").read_text()
ORCHESTRATOR_LINES = (PLATFORM_ROOT / "prompts" / "orchestrator.md").read_text().splitlines()
EVOLVE_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "evolve-pipeline.yml").read_text().splitlines()
)
DISTILL_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "distill-pipeline.yml").read_text().splitlines()
)
CLASSIFY_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "classify-pipeline.yml").read_text().splitlines()
)


def _line(n: int) -> str:
    return ORCHESTRATOR_LINES[n - 1]


def _evolve_pipeline_line(n: int) -> str:
    return EVOLVE_PIPELINE_LINES[n - 1]


TRIAGE_PIPELINE_LINES = (
    (PLATFORM_ROOT / ".github" / "workflows" / "triage-pipeline.yml").read_text().splitlines()
)


def _triage_pipeline_line(n: int) -> str:
    return TRIAGE_PIPELINE_LINES[n - 1]


def test_merge_shell_out_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:144" in LANES
    assert "agent merge <PR_NUMBER> --project target --check" in _line(144)


def test_auto_merge_range_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:156-167" in LANES
    assert "AUTO_MERGE" in _line(156)
    assert "Any condition failed" in _line(167)


def test_failure_handling_range_citation_matches_orchestrator() -> None:
    assert "lines 101-109" in LANES
    assert "Failure handling:" in _line(101)
    assert "VERDICT: REQUEST CHANGES" in _line(104)
    assert "reviewer feedback for the Implementer to act on." in _line(109)


def test_review_gate_range_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:85-96,104-109,160-161" in LANES
    assert "REVIEW GATE" in _line(85)
    assert "same rule Step 3 applies to a failed" in _line(96)
    assert "Tester verdict PASS and the Step 2A" in _line(160)
    assert "VERDICT: APPROVE" in _line(161)


def test_step2b_reviewer_repair_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:120-121" in LANES
    assert "REVIEWER (prompts/agents/reviewer.md)" in _line(121)


def test_evolve_pipeline_citation_matches_workflow() -> None:
    assert ".github/workflows/evolve-pipeline.yml:160" in LANES
    assert 'uv run agent evolve "$LANE"' in _evolve_pipeline_line(160)


def test_distill_pipeline_citation_matches_pipeline() -> None:
    assert ".github/workflows/distill-pipeline.yml:132" in LANES
    assert "uv run agent distill" in DISTILL_PIPELINE_LINES[132 - 1]


def test_classify_pipeline_citation_matches_pipeline() -> None:
    assert ".github/workflows/classify-pipeline.yml:118" in LANES
    assert "uv run agent triage" in CLASSIFY_PIPELINE_LINES[118 - 1]


def test_classify_override_citation_matches_orchestrator() -> None:
    assert "prompts/orchestrator.md:61-63" in LANES
    assert "Exception: `agent-ready`" in _line(61)
    assert "It does NOT override `needs-human`," in _line(63)


def test_classify_override_citation_matches_triage_pipeline() -> None:
    assert "triage-pipeline.yml:97-101" in LANES
    assert "needs-human" in _triage_pipeline_line(97)
    assert "blocked" in _triage_pipeline_line(97)
    assert "agent-ready" in _triage_pipeline_line(100)
    assert "approved-for-agent" in _triage_pipeline_line(100)


# --- Python citations: resolved by symbol, not by line number -------------

_DEF_OR_CLASS = re.compile(r"^(?:async\s+)?def\s+{name}\(|^class\s+{name}\b")
_MODULE_CONST = re.compile(r"^{name}\s*[:=]")
_CLASS_HEADER = re.compile(r"^class\s+{name}\b")
_CLASS_ATTR = re.compile(r"^\s+{name}\s*[:=]")


def resolve_symbol(source: str, symbol: str) -> list[int]:
    """Return the 0-indexed line numbers where `symbol` is defined in `source`.

    Plain names match a `def`/`class` header or a module-level constant
    assignment. A dotted `Class.attr` name first finds the (unique) class,
    then looks for the attribute assignment within that class's indented
    body only, so `attr` doesn't false-match a same-named field elsewhere.
    """
    lines = source.splitlines()
    if "." in symbol:
        class_name, attr = symbol.split(".", 1)
        header = re.compile(_CLASS_HEADER.pattern.format(name=re.escape(class_name)))
        class_lines = [i for i, line in enumerate(lines) if header.match(line)]
        if len(class_lines) != 1:
            return []
        start = class_lines[0]
        end = len(lines)
        for i in range(start + 1, len(lines)):
            # A column-0 comment inside the class body would also end the
            # scan here (non-blank, non-indented, same as a dedent). Legal
            # Python, but ruff-format never produces it, so it's not guarded
            # against.
            if lines[i].strip() and not lines[i][0].isspace():
                end = i
                break
        attr_re = re.compile(_CLASS_ATTR.pattern.format(name=re.escape(attr)))
        return [i for i in range(start + 1, end) if attr_re.match(lines[i])]

    def_re = re.compile(_DEF_OR_CLASS.pattern.format(name=re.escape(symbol)))
    const_re = re.compile(_MODULE_CONST.pattern.format(name=re.escape(symbol)))
    return [i for i, line in enumerate(lines) if def_re.match(line) or const_re.match(line)]


def test_resolve_symbol_no_match_returns_empty() -> None:
    assert resolve_symbol("x = 1\ny = 2\n", "missing") == []


def test_resolve_symbol_duplicate_definition_returns_both() -> None:
    source = "def dupe():\n    pass\n\n\ndef dupe():\n    pass\n"
    assert resolve_symbol(source, "dupe") == [0, 4]


def test_resolve_symbol_survives_lines_inserted_above() -> None:
    source = "\n" * 50 + "def target():\n    pass\n"
    matches = resolve_symbol(source, "target")
    assert matches == [50]


def test_resolve_symbol_does_not_confuse_prefix_names() -> None:
    source = "def run_merge():\n    pass\n\n\ndef run_merge_check():\n    pass\n"
    assert resolve_symbol(source, "run_merge") == [0]
    assert resolve_symbol(source, "run_merge_check") == [4]


def test_resolve_symbol_dotted_attribute_scoped_to_its_class() -> None:
    source = (
        "class Other(Base):\n"
        "    auto_merge: bool = True\n"
        "\n\n"
        "class LoopConfig(Base):\n"
        "    max_attempts: int = 3\n"
        "    auto_merge: bool = False\n"
        "\n\n"
        "class Trailer(Base):\n"
        "    pass\n"
    )
    assert resolve_symbol(source, "LoopConfig.auto_merge") == [6]
    assert resolve_symbol(source, "Other.auto_merge") == [1]


_NUMBERED_PYTHON_CITATION_RE = re.compile(r"[\w/.-]+\.py:\d+")


def test_no_numbered_python_citations_in_lanes() -> None:
    numbered = _NUMBERED_PYTHON_CITATION_RE.findall(LANES)
    assert numbered == [], f"line-numbered Python citations found: {numbered}"


def test_numbered_python_citation_ban_catches_short_form() -> None:
    # Regression for the gap the ban used to have: anchoring on
    # `src/agent_ops/` missed short-form citations like `workflows/foo.py:123`
    # -- the table cites Python files that way, and it's exactly the fragile
    # citation shape #250 retires.
    assert _NUMBERED_PYTHON_CITATION_RE.findall("see workflows/groom.py:123 for detail") == [
        "workflows/groom.py:123"
    ]


_REVERSED_PYTHON_CITATION_RE = re.compile(r"`([^`/]+)`\s*\(`([^`]*\.py)`\)")


def test_no_reversed_order_python_citations_in_lanes() -> None:
    # The canonical order is `path.py` (`symbol`) -- extract_python_citations
    # (and thus every resolution test below) only ever looks for that shape.
    # A citation written the other way round, `symbol` (`path.py`), is
    # invisible to that extraction: it never resolves, and it never counts
    # toward the floor either, so nothing here would fail on its own. That's
    # exactly how three of them slipped in undetected. This test matches the
    # reversed shape directly so writing one fails loudly instead of quietly.
    reversed_citations = _REVERSED_PYTHON_CITATION_RE.findall(LANES)
    assert reversed_citations == [], (
        "citation(s) in the wrong order -- use `path.py` (`symbol`), not "
        f"`symbol` (`path.py`): {reversed_citations}"
    )


def test_reversed_python_citation_ban_catches_reversed_form() -> None:
    found = _REVERSED_PYTHON_CITATION_RE.findall(
        "checked inside `run_implement` (`src/agent_ops/workflows/implement.py`) to decide"
    )
    assert found == [("run_implement", "src/agent_ops/workflows/implement.py")]
    # A canonical citation, path first, must never trip the same ban.
    assert (
        _REVERSED_PYTHON_CITATION_RE.findall(
            "`src/agent_ops/workflows/implement.py` (`run_implement`)"
        )
        == []
    )


_PYTHON_CITATION_RE = re.compile(r"`src/agent_ops/([^`]+)`\s*\(`([^`]+)`\)")


def extract_python_citations(text: str) -> list[tuple[str, str]]:
    """Every (relative path under src/agent_ops/, symbol) citation lanes.md contains.

    Parametrizing over this instead of a hand-maintained list means there is
    no second copy of the citation set that can drift from the doc -- the
    kind of duplication-plus-gate #250 exists to remove. The tradeoff: if the
    doc's citation format ever stops matching this regex (a reformatted
    table, a restructured heading), extraction silently returns nothing and
    the parametrized test below would pass having checked zero citations.
    test_python_citation_floor guards against exactly that.
    """
    return sorted(set(_PYTHON_CITATION_RE.findall(text)))


PYTHON_CITATIONS = extract_python_citations(LANES)

# lanes.md cites 28 distinct Python symbols today. If extraction ever yields
# fewer, either a citation was silently dropped from the doc or the regex
# above stopped matching its format -- both are real drift. Raising this
# number (after adding citations) or lowering it (after deliberately
# removing some) is an intentional edit to make alongside the doc change,
# never a quick fix for a test that's gone red.
_MIN_PYTHON_CITATIONS = 28


def test_python_citation_floor() -> None:
    assert len(PYTHON_CITATIONS) >= _MIN_PYTHON_CITATIONS, (
        f"only found {len(PYTHON_CITATIONS)} Python citations in lanes.md, expected at "
        f"least {_MIN_PYTHON_CITATIONS} -- the extraction regex may have stopped matching "
        "the doc's citation format"
    )


@pytest.mark.parametrize("relative_path,symbol", PYTHON_CITATIONS)
def test_lanes_citation_resolves_uniquely(relative_path: str, symbol: str) -> None:
    full_path = f"src/agent_ops/{relative_path}"
    source = (PLATFORM_ROOT / "src" / "agent_ops" / relative_path).read_text()
    matches = resolve_symbol(source, symbol)
    assert len(matches) == 1, (
        f"`{symbol}` is not defined exactly once in {full_path}: {len(matches)} matches"
    )

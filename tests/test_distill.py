from pathlib import Path

import pytest

from agent_ops import worktree
from agent_ops.config import DistillConfig
from agent_ops.runtimes.base import RunRequest
from agent_ops.utils import CommandError
from agent_ops.workflows import distill as distill_module
from agent_ops.workflows.distill import (
    changed_files_ok,
    parse_distill,
    protected_sections_changed,
    prunable_sections,
    run_distill,
    unresolved_protected_sections,
)

PROTECTED = DistillConfig().protected_sections


def _template_agents_md() -> str:
    return "\n".join(f"## {h}\n\nSome content for {h}.\n" for h in PROTECTED)


def _write_config(tmp_path: Path, body: str) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "config.yaml").write_text(body)


# ---------- prunable_sections ----------


def test_template_shaped_file_has_no_prunable_sections() -> None:
    assert prunable_sections(_template_agents_md(), PROTECTED) == []


def test_an_appended_section_is_the_only_prunable_one() -> None:
    text = _template_agents_md() + "\n## Run log\n\nran the tests, all green.\n"
    assert [h for h, _ in prunable_sections(text, PROTECTED)] == ["Run log"]


def test_heading_match_is_case_and_whitespace_insensitive() -> None:
    text = "##   danger ZONES  \n\nbody\n"
    assert prunable_sections(text, ["Danger zones"]) == []


def test_content_above_the_first_heading_is_never_prunable() -> None:
    text = "# AGENTS.md\n\nUnheaded intro prose.\n\n## Run log\n\nnotes\n"
    sections = prunable_sections(text, [])
    assert [h for h, _ in sections] == ["Run log"]
    assert "intro" not in "".join(b for _, b in sections)


def test_a_preamble_bearing_file_keeps_the_preamble_out_of_prunable_sections() -> None:
    text = (
        "# AGENTS.md\n\n"
        "Instructions for coding agents. Keep this file short, current, and "
        "ruthlessly project-specific.\n\n"
        "## Run log\n\nnotes\n"
    )
    sections = prunable_sections(text, [])
    assert [h for h, _ in sections] == ["Run log"]


# ---------- unresolved_protected_sections ----------


def test_unresolved_protected_sections_flags_a_renamed_heading() -> None:
    text = "## Danger zones\n\nbody\n"
    assert unresolved_protected_sections(text, ["Danger zones", "Danger zones and gotchas"]) == [
        "Danger zones and gotchas"
    ]


def test_unresolved_protected_sections_is_empty_when_every_name_resolves() -> None:
    text = "## Danger zones\n\nbody\n\n## Run log\n\nnotes\n"
    assert unresolved_protected_sections(text, ["Danger zones"]) == []


# ---------- parse_distill ----------


def test_parses_multiple_cuts() -> None:
    text = """Distilled the file.

DISTILL REPORT:
Run log — three "tests green" entries from June — superseded by the CI badge
Conventions — duplicate commit-style note — already stated once above
"""
    cuts = parse_distill(text)
    assert cuts is not None
    assert [(c.section, c.reason) for c in cuts] == [
        ("Run log", "superseded by the CI badge"),
        ("Conventions", "already stated once above"),
    ]


def test_explicit_none_is_empty_list() -> None:
    assert parse_distill("nothing to prune\n\nDISTILL REPORT:\nnone\n") == []


def test_no_marker_is_none() -> None:
    assert parse_distill("no block here") is None


def test_uses_last_marker_and_ignores_trailing_prose() -> None:
    text = (
        "DISTILL REPORT:\nRun log — draft — draft reason\n"
        "DISTILL REPORT:\nRun log — final — final reason\nnoise after\n"
    )
    cuts = parse_distill(text)
    assert cuts is not None
    assert [(c.cut, c.reason) for c in cuts] == [("final", "final reason")]


# ---------- pure guards ----------


def test_changed_files_ok_accepts_only_agents_md_or_nothing() -> None:
    assert changed_files_ok([])
    assert changed_files_ok(["AGENTS.md"])
    assert not changed_files_ok(["AGENTS.md", "README.md"])
    assert not changed_files_ok(["README.md"])


def test_protected_sections_changed_flags_a_modified_protected_section() -> None:
    before = "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    after = "## Danger zones\n\nNever touch auth or payments.\n\n## Run log\n\nnew notes\n"
    assert protected_sections_changed(before, after, ["Danger zones"]) == ["Danger zones"]


def test_protected_sections_changed_is_quiet_when_only_prunable_sections_move() -> None:
    before = "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    after = "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    assert protected_sections_changed(before, after, ["Danger zones"]) == []


def test_protected_sections_changed_flags_a_modified_preamble() -> None:
    before = "# AGENTS.md\n\nInstructions for coding agents.\n\n## Run log\n\nold notes\n"
    after = "## Run log\n\nold notes\n"
    assert protected_sections_changed(before, after, []) == ["preamble"]


def test_protected_sections_changed_is_quiet_when_the_preamble_is_untouched() -> None:
    before = "# AGENTS.md\n\nInstructions for coding agents.\n\n## Run log\n\nold notes\n"
    after = "# AGENTS.md\n\nInstructions for coding agents.\n\n## Run log\n\nnew notes\n"
    assert protected_sections_changed(before, after, []) == []


# ---------- run_distill pre-checks (no agent must run) ----------


def _fail(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("must not run an agent for a no-op distill")


def test_missing_agents_md_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(distill_module, "role_request", _fail)
    monkeypatch.setattr(worktree, "create", _fail)

    logged: list[str] = []
    assert run_distill(tmp_path, log=logged.append) == []
    assert any("no AGENTS.md" in line for line in logged)


def test_a_lean_file_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "AGENTS.md").write_text("## Run log\n\nshort\n")
    monkeypatch.setattr(distill_module, "role_request", _fail)
    monkeypatch.setattr(worktree, "create", _fail)

    logged: list[str] = []
    assert run_distill(tmp_path, log=logged.append) == []
    assert any("lean" in line for line in logged)


def test_a_file_where_every_section_is_protected_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, "distill:\n  min_lines: 1\n")
    (tmp_path / "AGENTS.md").write_text(_template_agents_md())
    monkeypatch.setattr(distill_module, "role_request", _fail)
    monkeypatch.setattr(worktree, "create", _fail)

    logged: list[str] = []
    assert run_distill(tmp_path, log=logged.append) == []
    assert any("every section is protected" in line for line in logged)


# ---------- run_distill against a stubbed agent + git ----------


class _FakeRuntime:
    name = "fake"

    def __init__(self, text: str, *, edit: tuple[Path, str] | None = None) -> None:
        self.text = text
        self.edit = edit

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest):
        from agent_ops.runtimes.base import RunResult

        # Mimics a real agent editing AGENTS.md as a side effect of the run,
        # so run_distill's "before" read (right after worktree.create) and
        # "after" read (once this returns) can actually differ — needed to
        # exercise the protected-section guard against a real change.
        if self.edit is not None:
            path, content = self.edit
            path.write_text(content)
        return RunResult(ok=True, text=self.text)

    def classify_failure(self, result):
        from agent_ops.runtimes.base import FailureKind

        return FailureKind.AGENT_FAILURE


class _FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _project(tmp_path: Path, *, min_lines: int = 1) -> tuple[Path, Path]:
    """A tmp project root with a short prunable AGENTS.md, and its worktree dir.

    Overrides `protected_sections` to just "Danger zones" — the only heading
    this minimal fixture actually has — so `unresolved_protected_sections`
    doesn't bail on the other five default template headings the fixture
    omits.
    """
    _write_config(
        tmp_path,
        f"distill:\n  min_lines: {min_lines}\n  protected_sections:\n    - Danger zones\n",
    )
    original = "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    (tmp_path / "AGENTS.md").write_text(original)
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    return tmp_path, wt_path


def _stub_distill_run(
    monkeypatch: pytest.MonkeyPatch,
    wt_path: Path,
    *,
    report_text: str,
    diff_names: list[str],
    after_agents_md: str | None = None,
    existing_prs: list[dict] | None = None,
) -> list[list[str]]:
    """Drive run_distill against a fake agent + fake git; return every argv `run` saw.

    `wt_path`'s AGENTS.md, as written by the caller before this is called, is
    the worktree's *pre*-agent content (what `worktree.create` would have
    checked out). `after_agents_md`, if given, is written by the fake agent's
    `.run()` — the *post*-edit content — so a test can make the two differ.
    """
    calls: list[list[str]] = []
    status_z = "".join(f" M {n}\0" for n in diff_names)

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        calls.append(cmd)
        if cmd[:4] == ["git", "status", "--porcelain", "-z"]:
            return _FakeProc(status_z)
        return _FakeProc("")

    edit = (wt_path / "AGENTS.md", after_agents_md) if after_agents_md is not None else None

    def fake_role_request(config, role_name, prompt, cwd, **kwargs):
        return _FakeRuntime(report_text, edit=edit), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(distill_module, "run", fake_run)
    monkeypatch.setattr(distill_module, "role_request", fake_role_request)
    monkeypatch.setattr(distill_module.github, "open_prs", lambda *a, **k: existing_prs or [])
    monkeypatch.setattr(worktree, "create", lambda *a, **k: wt_path)
    monkeypatch.setattr(worktree, "remove", lambda *a, **k: None)
    return calls


def test_a_diff_touching_more_than_agents_md_raises_and_does_not_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md", "README.md"],
    )

    with pytest.raises(RuntimeError, match="more than AGENTS.md"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


def test_a_changed_protected_section_raises_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
        after_agents_md=(
            "## Danger zones\n\nNever touch auth or payments.\n\n## Run log\n\nnew notes\n"
        ),
    )

    with pytest.raises(RuntimeError, match="Danger zones"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


def test_a_changed_preamble_raises_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    preamble = "# AGENTS.md\n\nInstructions for coding agents.\n\n"
    (wt_path / "AGENTS.md").write_text(
        preamble + "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
        after_agents_md=("## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"),
    )

    with pytest.raises(RuntimeError, match="preamble"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


def test_a_protected_section_that_does_not_exist_bails_before_any_agent_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    _write_config(
        root,
        "distill:\n  min_lines: 1\n  protected_sections:\n    - Danger zones and gotchas\n",
    )
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    )

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        return _FakeProc("")

    monkeypatch.setattr(distill_module, "run", fake_run)
    monkeypatch.setattr(distill_module.github, "open_prs", lambda *a, **k: [])
    monkeypatch.setattr(worktree, "create", lambda *a, **k: wt_path)
    monkeypatch.setattr(worktree, "remove", lambda *a, **k: None)
    monkeypatch.setattr(distill_module, "role_request", _fail)

    with pytest.raises(RuntimeError, match="Danger zones and gotchas"):
        run_distill(root, log=lambda _msg: None)


def test_an_empty_report_makes_no_commit_and_opens_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nold notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch, wt_path, report_text="DISTILL REPORT:\nnone\n", diff_names=[]
    )

    def _no_pr(*_a: object, **_k: object) -> str:
        raise AssertionError("must not open a PR for an empty report")

    monkeypatch.setattr(distill_module.github, "create_pr", _no_pr)

    assert run_distill(root, log=lambda _msg: None) == []
    assert not any(cmd[:2] == ["git", "commit"] for cmd in calls)


def test_auto_merge_is_never_applied_to_a_distill_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """distill.py never imports `merge`, so the real guard is: no `gh pr merge`, no `--auto`.

    Monkeypatching `merge_module.run_merge` (the previous version of this
    test) would keep passing even if the code started merging via a raw `gh`
    call — it asserts against a mechanism the code doesn't use.
    """
    root, wt_path = _project(tmp_path)
    _write_config(
        root,
        "distill:\n  min_lines: 1\n  protected_sections:\n    - Danger zones\nloop:\n"
        "  auto_merge: true\n",
    )
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
    )
    monkeypatch.setattr(distill_module.github, "create_pr", lambda *a, **k: "https://example/pr/1")

    cuts = run_distill(root, log=lambda _msg: None)

    assert [c.section for c in cuts] == ["Run log"]
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert not any("--auto" in cmd for cmd in calls)


# ---------- finding 2: an unparseable report must never look like success ----------


def test_an_unparseable_report_with_a_real_edit_raises_instead_of_vanishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    # an en dash (–, U+2013) is outside _CUT_LINE_SEP's character class, so
    # this line fails to parse as a cut — with the old code that silently
    # became an empty cuts list, "nothing to distill", and the edit above was
    # discarded when the worktree was force-removed.
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log – trimmed old notes – stale\n",
        diff_names=["AGENTS.md"],
    )

    with pytest.raises(RuntimeError, match="no parseable report"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


def test_an_explicit_none_report_with_an_actual_edit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch, wt_path, report_text="DISTILL REPORT:\nnone\n", diff_names=["AGENTS.md"]
    )

    with pytest.raises(RuntimeError, match="says 'none'"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


# ---------- finding 4: untracked files must count toward the AGENTS.md-only guard ----------


def test_an_untracked_relocation_file_fails_the_agents_md_only_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\ntrimmed\n"
    )
    (wt_path / "AGENTS-archive.md").write_text("relocated content\n")
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — moved to archive — relocated\n",
        # `git status --porcelain -z` would report the new untracked file too
        # — `git diff --name-only` never would, which is the bug.
        diff_names=["AGENTS.md", "AGENTS-archive.md"],
    )

    with pytest.raises(RuntimeError, match="more than AGENTS.md"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


# ---------- finding 3: the protected-section baseline must be the worktree's copy ----------


def test_protected_section_baseline_is_the_worktree_copy_not_the_project_root_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    # The worktree is cut from origin/<base>, which has moved on from
    # project_root's own checkout — its Danger zones wording already differs,
    # with no agent involved. Comparing against project_root's copy would
    # flag that pre-existing divergence as a protected-section violation, even
    # though the agent leaves Danger zones untouched (only Run log changes).
    diverged = "## Danger zones\n\nNever touch auth or payments.\n\n## Run log\n\n{}\n"
    (wt_path / "AGENTS.md").write_text(diverged.format("old notes"))
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
        after_agents_md=diverged.format("trimmed"),
    )
    monkeypatch.setattr(distill_module.github, "create_pr", lambda *a, **k: "https://example/pr/1")

    cuts = run_distill(root, log=lambda _msg: None)

    assert [c.section for c in cuts] == ["Run log"]
    assert any(cmd[:2] == ["git", "push"] for cmd in calls)


# ---------- finding 1: re-runs must not collide ----------


def test_running_distill_again_bails_while_the_first_pr_is_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
    )
    monkeypatch.setattr(distill_module.github, "create_pr", lambda *a, **k: "https://example/pr/1")

    first = run_distill(root, log=lambda _msg: None)
    assert [c.section for c in first] == ["Run log"]

    # A second run against the same repo finds the first run's PR still open.
    monkeypatch.setattr(
        distill_module.github,
        "open_prs",
        lambda *a, **k: [
            {"number": 7, "url": "https://example/pr/7", "headRefName": "docs/distill-abc1234"}
        ],
    )
    logged: list[str] = []
    second = run_distill(root, log=logged.append)

    assert second == []
    assert any("already exists" in line for line in logged)


# ---------- finding 5: a heading inside a fenced code block is not a section boundary ----------


def test_a_heading_inside_a_fenced_code_block_does_not_fork_a_new_section() -> None:
    text = (
        "## Danger zones\n\n"
        "Never touch auth. Example:\n\n"
        "```\n"
        "## fake heading\n"
        "not real\n"
        "```\n\n"
        "Still part of Danger zones.\n\n"
        "## Run log\n\nnotes\n"
    )
    sections = prunable_sections(text, ["Danger zones"])
    assert [h for h, _ in sections] == ["Run log"]


# ---------- AGENTS.md missing from the worktree must not raise a bare traceback ----------


def test_agents_md_missing_from_the_worktree_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS.md can exist at project_root but not on origin/<base> — e.g. it
    is untracked, or only committed on a feature branch. The worktree is cut
    from origin/<base>, so its copy is missing even though the pre-checks
    (which read project_root's copy) passed.
    """
    root, wt_path = _project(tmp_path)
    # `_project` only writes project_root's AGENTS.md, not the worktree's — the
    # worktree here has none, exactly like a fresh checkout of a base branch
    # that never got the file committed.
    calls = _stub_distill_run(
        monkeypatch, wt_path, report_text="DISTILL REPORT:\nnone\n", diff_names=[]
    )

    with pytest.raises(RuntimeError, match="AGENTS.md is not on"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


# ---------- a missing `gh` binary must fail closed, not push an orphan branch ----------


def test_a_missing_gh_binary_during_the_stale_pr_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`github.open_prs` shells out to `gh`, and `utils.run` lets a missing
    binary's FileNotFoundError through regardless of `check` (agent-ops#154,
    not fixed here). `github.create_pr` shells out to the same binary, so a
    `gh`-less box cannot possibly finish a run — it must fail here, before a
    worktree, an agent run, a commit, and a push are all spent on a run that
    would otherwise die at `create_pr` and orphan the pushed branch.
    """
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
    )

    def _raise_missing_gh(*_a: object, **_k: object) -> list[dict]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(distill_module.github, "open_prs", _raise_missing_gh)
    monkeypatch.setattr(distill_module.github, "create_pr", _raise_missing_gh)

    with pytest.raises(RuntimeError, match="gh"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)


def test_a_failing_gh_command_during_the_stale_pr_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CommandError means `gh` ran and exited non-zero (rate limit, expired
    auth, a network blip) — distinct from the missing-binary OSError case
    above, and exactly the case this used to swallow (`stale_prs = []`)
    before fail-closed was pinned. A regression back to that fail-open
    handling would let the run reach `create_pr`, so that path is wired to
    raise loudly instead of letting the test pass on a silent success.
    """
    root, wt_path = _project(tmp_path)
    (wt_path / "AGENTS.md").write_text(
        "## Danger zones\n\nNever touch auth.\n\n## Run log\n\nnew notes\n"
    )
    calls = _stub_distill_run(
        monkeypatch,
        wt_path,
        report_text="DISTILL REPORT:\nRun log — trimmed old notes — stale\n",
        diff_names=["AGENTS.md"],
    )

    def _raise_command_error(*_a: object, **_k: object) -> list[dict]:
        raise CommandError("gh: rate limit exceeded")

    def _must_not_reach_create_pr(*_a: object, **_k: object) -> str:
        raise AssertionError(
            "must not reach create_pr: the stale-PR check should fail closed first"
        )

    monkeypatch.setattr(distill_module.github, "open_prs", _raise_command_error)
    monkeypatch.setattr(distill_module.github, "create_pr", _must_not_reach_create_pr)

    with pytest.raises(RuntimeError, match="gh"):
        run_distill(root, log=lambda _msg: None)

    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)

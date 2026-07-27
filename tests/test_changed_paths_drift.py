"""Anti-drift guard, alongside `test_stub_drift.py` and
`test_gate_label_descriptions_match_what_the_workflow_yamls_create` in
`test_triage.py`: those exist because a fact with exactly one correct
implementation keeps getting reimplemented, slightly wrong, by a lane that
didn't know a correct version already existed. Add a row here only when a
second instance of *this* invariant (or a genuinely new one) appears --
speculative generalisation defeats the point.

#150: the auto-merge size cap was enforced in tested code, then restated as
CI-lane prose that silently diverged from it after #136. #152: Evolve wrote
`git diff --name-only` to decide what a run touched, which -- unlike the
`git status --porcelain -z` `worktree.changed_paths` (worktree.py:144) uses
-- compares the working tree to the index, so it is blind to a staged edit
and never lists an untracked file. `git commit -m` commits the whole index,
so a staged-but-uncommitted edit to a blocked path (e.g.
`prompts/orchestrator.md`) would pass a diff-based allowlist check, reach the
PR, and be accompanied by a PR body asserting that file could not have been
touched. Caught in self-review on that branch, never reached `main`.

This test pins `git status --porcelain` to `worktree.py` (where
`changed_paths` and the worktree-dirty check both live) so every other
module must go through the shared helper, and bans `git diff --name-only`
written as a literal in a shelled-out command -- a list literal, a joined
string, or `.split()` on one, however split across lines. It does NOT catch
a command assembled from parts elsewhere (e.g. a `CMD = [...]` constant
built at module scope and passed by reference) -- the AST walk only inspects
what is written directly at the call site, and extending it further is
deliberately out of scope (see the header above on speculative
generalisation).
"""

from __future__ import annotations

import ast
from pathlib import Path

from agent_ops.utils import PLATFORM_ROOT

FAILURE_EXPLANATION = """
`git diff --name-only` compares the working tree to the index: it is blind
to a staged-but-uncommitted edit and never lists an untracked file. `git
commit -m` commits the whole index, so a staged edit to a blocked path (e.g.
prompts/orchestrator.md) would pass a diff-based allowlist check, reach the
PR, and ship with a PR body asserting that file could not have been
touched -- this is exactly the #152 regression this test pins against
recurrence (see also #150, the same failure shape for a different check).

`worktree.changed_paths` (worktree.py) is the one correct implementation:
it shells `git status --porcelain -z`, which sees staged, unstaged, AND
untracked paths. Import and call it instead of shelling git yourself.

The one legitimate exception this test does not cover: diffing two
*committed* refs (`git diff --name-only base..head`) answers a different
question -- "what changed between these commits" rather than "what has
this working tree changed" -- and would warrant consciously extending this
test, not routing around it.
""".strip()


def _call_string_args(call: ast.Call) -> list[str]:
    """String constants passed directly as args/kwargs to `call`, plus the
    string `call` itself is a method call on (covers `"git diff --name-only".split()`).

    Descends into list/tuple/set literals (covers `run(["git", "status", ...])`)
    but not into nested calls -- those are visited on their own as `ast.walk`
    reaches them, so descending here would double-count them.
    """
    strings: list[str] = []

    def _collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                _collect(elt)

    if isinstance(call.func, ast.Attribute):
        _collect(call.func.value)
    for arg in call.args:
        _collect(arg)
    for keyword in call.keywords:
        if keyword.value is not None:
            _collect(keyword.value)
    return strings


def _has_pair(strings: list[str], first: str, second: str) -> bool:
    """True if `first` and `second` appear as separate args, or together in one."""
    if first in strings and second in strings:
        return True
    return any(first in s and second in s for s in strings)


def _banned_git_calls(root: Path) -> list[str]:
    """Every `path:line` under `root` that shells a changed-paths check we've

    already agreed has exactly one correct implementation.
    """
    violations = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            strings = _call_string_args(node)
            is_name_only_diff = _has_pair(strings, "diff", "--name-only")
            is_stray_porcelain = path.name != "worktree.py" and _has_pair(
                strings, "status", "--porcelain"
            )
            if is_name_only_diff or is_stray_porcelain:
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    return violations


def test_no_git_diff_name_only_or_stray_porcelain_calls(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def touched(cwd):\n"
        "    run(['git', 'diff', '--stat'], cwd=cwd)\n"
        "    return worktree.changed_paths(cwd)\n"
    )
    (tmp_path / "worktree.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def changed_paths(cwd):\n"
        "    run(['git', 'status', '--porcelain', '-z'], cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_list_form_name_only_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def touched(cwd):\n"
        "    run(['git', 'diff', '--name-only'], cwd=cwd)\n"
    )

    violations = _banned_git_calls(tmp_path)

    assert violations == ["bad.py:4"]


def test_name_only_split_across_lines_is_still_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def touched(cwd):\n"
        "    run(\n"
        "        [\n"
        "            'git',\n"
        "            'diff',\n"
        "            '--name-only',\n"
        "        ],\n"
        "        cwd=cwd,\n"
        "    )\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_shell_string_name_only_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import subprocess\n"
        "\n"
        "def touched(cwd):\n"
        "    subprocess.run('git diff --name-only', shell=True, cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_split_string_name_only_is_flagged(tmp_path: Path) -> None:
    """`"git diff --name-only".split()` puts the string in `call.func.value`,
    not `call.args` -- a separate code path from the list-literal form above.
    """
    (tmp_path / "bad.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def touched(cwd):\n"
        "    run('git diff --name-only'.split(), cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_porcelain_outside_worktree_py_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "distill.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def touched(cwd):\n"
        "    run(['git', 'status', '--porcelain', '-z'], cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == ["distill.py:4"]


def test_porcelain_inside_worktree_py_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "worktree.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def is_dirty(cwd):\n"
        "    return bool(run(['git', 'status', '--porcelain'], cwd=cwd).stdout)\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_a_docstring_or_comment_mention_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text(
        '"""Uses git status --porcelain (not git diff --name-only) to see\n'
        'untracked files."""\n'
        "\n"
        "# git diff --name-only would miss staged edits\n"
        "def touched():\n"
        "    pass\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_diff_stat_is_not_flagged(tmp_path: Path) -> None:
    """`git diff --stat` is a display-only summary, not a changed-paths check."""
    (tmp_path / "clean.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def summary(cwd):\n"
        "    return run(['git', 'diff', '--stat'], cwd=cwd).stdout\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_src_has_no_banned_git_calls() -> None:
    violations = _banned_git_calls(PLATFORM_ROOT / "src" / "agent_ops")

    assert violations == [], (
        f"Found changed-paths checks that reimplement a solved problem: {violations}\n\n"
        f"{FAILURE_EXPLANATION}"
    )

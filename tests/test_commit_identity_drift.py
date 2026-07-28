"""Anti-drift guard, alongside `test_changed_paths_drift.py` and
`test_stub_drift.py`: those exist because a fact with exactly one correct
implementation keeps getting reimplemented, slightly wrong, by a lane that
didn't know a correct version already existed.

#203: `run_distill` and `run_evolve` both committed with a plain `git commit`.
That works locally, where a developer's `user.name`/`user.email` are already
configured, but GitHub-hosted runners configure neither — so a CI-lane
pipeline checks out, installs uv and the agent CLI, spends a full agent
session, and only then dies at commit with "unable to auto-detect email
address". `worktree.commit` (worktree.py) is the one correct implementation:
it falls back to a bot identity only when git has none configured, and never
overrides a developer's local identity. This test pins `git commit` to
`worktree.py` so the next committing pipeline can't forget the fallback the
way #153 and #198 each had to add it by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agent_ops.utils import PLATFORM_ROOT

FAILURE_EXPLANATION = """
GitHub-hosted runners configure neither `user.name` nor `user.email`. A
plain `git commit` there fails with "unable to auto-detect email address" —
only after a CI-lane pipeline already paid for checkout, setup, and a full
agent run. This is exactly the #203 regression this test pins against
recurrence (#153 and #198 each had to discover and fix it independently).

`worktree.commit` (worktree.py) is the one correct implementation: it probes
`git var GIT_AUTHOR_IDENT` and only injects a bot identity when git has none
configured, so a local `agent distill` / `agent evolve` still commits as the
developer. Call it instead of shelling `git commit` yourself.
""".strip()


def _call_string_args(call: ast.Call) -> list[str]:
    """String constants passed directly as args/kwargs to `call`.

    Descends into list/tuple/set literals (covers `run(["git", "commit", ...])`)
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
    """Every `path:line` under `root` that shells a `git commit` outside `worktree.py`."""
    violations = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if path.name == "worktree.py":
                continue
            strings = _call_string_args(node)
            if _has_pair(strings, "commit", "-m"):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    return violations


def test_no_stray_git_commit_calls(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text(
        "from agent_ops import worktree\n\ndef land(cwd):\n    worktree.commit(cwd, 'a message')\n"
    )
    (tmp_path / "worktree.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def commit(cwd, message):\n"
        "    run(['git', 'commit', '-m', message], cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_list_form_commit_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def land(cwd):\n"
        "    run(['git', 'commit', '-m', 'a message'], cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_commit_split_across_lines_is_still_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def land(cwd, message):\n"
        "    run(\n"
        "        [\n"
        "            'git',\n"
        "            'commit',\n"
        "            '-m',\n"
        "            message,\n"
        "        ],\n"
        "        cwd=cwd,\n"
        "    )\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_shell_string_commit_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import subprocess\n"
        "\n"
        "def land(cwd):\n"
        "    subprocess.run('git commit -m message', shell=True, cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == ["bad.py:4"]


def test_commit_inside_worktree_py_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "worktree.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def commit(cwd, message):\n"
        "    run(['git', 'commit', '-m', message], cwd=cwd)\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_a_docstring_or_comment_mention_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text(
        '"""Commits go through worktree.commit (git commit -m under the hood),\n'
        'never a raw git commit here."""\n'
        "\n"
        "# git commit -m would skip the CI identity fallback\n"
        "def land():\n"
        "    pass\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_git_log_is_not_flagged(tmp_path: Path) -> None:
    """`git log --pretty=%s` etc. never pair "commit" with "-m" — not a commit call."""
    (tmp_path / "clean.py").write_text(
        "from agent_ops.utils import run\n"
        "\n"
        "def subjects(cwd):\n"
        "    return run(['git', 'log', 'origin/a..origin/b', '--pretty=%s'], cwd=cwd).stdout\n"
    )

    assert _banned_git_calls(tmp_path) == []


def test_src_has_no_stray_git_commit_calls() -> None:
    violations = _banned_git_calls(PLATFORM_ROOT / "src" / "agent_ops")

    assert violations == [], (
        "Found a raw `git commit` outside worktree.py:\n"
        + "\n".join(violations)
        + "\n\n"
        + FAILURE_EXPLANATION
    )

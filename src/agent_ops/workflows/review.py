from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from agent_ops import github
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.utils import CommandError
from agent_ops.workflows.implement import role_request


def budget_diff(diff: str, max_lines: int) -> tuple[str, list[tuple[str, int]]]:
    """Cap a unified diff to `max_lines` total lines, dropping whole-file chunks.

    Splits the diff on `^diff --git ` boundaries (one chunk per file). If the
    diff is already within budget it passes through unchanged. Otherwise the
    largest chunks are dropped first (path ascending breaks size ties, for
    determinism) until the total is back under budget. Returns
    `(kept_diff, omitted)` where `omitted` is `[(path, line_count), ...]` in
    the order chunks were dropped.
    """
    if not diff:
        return "", []

    parts = re.split(r"(?m)^(?=diff --git )", diff)
    chunks = [part for part in parts if part]
    if not chunks:
        # no recognizable per-file header (shouldn't happen for `gh pr diff`
        # output) — nothing we can safely drop, so pass it through as-is.
        return diff, []

    entries: list[tuple[str, int, str]] = []
    for chunk in chunks:
        header = chunk.splitlines()[0]
        match = re.match(r"^diff --git a/(\S+) b/(\S+)", header)
        path = match.group(2) if match else header.strip()
        entries.append((path, len(chunk.splitlines()), chunk))

    total = sum(count for _, count, _ in entries)
    if total <= max_lines:
        return diff, []

    # largest first; path ascending breaks size ties for a deterministic order
    drop_order = sorted(range(len(entries)), key=lambda i: (-entries[i][1], entries[i][0]))
    dropped: set[int] = set()
    omitted: list[tuple[str, int]] = []
    remaining = total
    for i in drop_order:
        if remaining <= max_lines:
            break
        path, count, _ = entries[i]
        dropped.add(i)
        omitted.append((path, count))
        remaining -= count

    kept_diff = "".join(chunk for i, (_, _, chunk) in enumerate(entries) if i not in dropped)
    return kept_diff, omitted


def run_review(
    project_root: Path,
    pr_number: int,
    *,
    runtime_name: str | None = None,
    post_comment: bool = False,
    log: Callable[[str], None] = print,
) -> str:
    """Run the reviewer role (read-only) over a PR diff; optionally post the result."""
    config = load_project_config(project_root)
    pr = github.pr_view(pr_number, cwd=project_root)
    diff = github.pr_diff(pr_number, cwd=project_root)

    total_lines = len(diff.splitlines())
    max_diff_lines = config.review.max_diff_lines
    diff, omitted = budget_diff(diff, max_diff_lines)
    if omitted:
        if not diff.strip():
            raise CommandError(
                f"PR #{pr_number}: diff is {total_lines} lines, over the "
                f"{max_diff_lines}-line review budget, and no single file fits "
                "within it — refusing to spend tokens on a review that can't see the change"
            )
        omitted_lines = "\n".join(f"- {path} ({count} lines)" for path, count in omitted)
        note = (
            f"NOTE: {len(omitted)} file(s) omitted from this diff "
            f"(over the {max_diff_lines}-line review budget):\n{omitted_lines}"
        )
        log(note)
        diff = f"{note}\n\n{diff}"

    prompt = render_task(
        "review",
        diff=diff,
        context=f"PR #{pr['number']}: {pr['title']}\n\n{pr.get('body') or ''}",
    )

    runtime, request = role_request(
        config, "reviewer", prompt, project_root, runtime_override=runtime_name
    )
    result = runtime.run(request)
    if not result.ok:
        raise RuntimeError(f"Review run failed: {result.text}")

    if post_comment:
        github.comment_on_pr(pr_number, f"## Agent review\n\n{result.text}", cwd=project_root)
        log(f"posted review comment on PR #{pr_number}")
    return result.text

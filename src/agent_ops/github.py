from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops.utils import CommandError, run

# What a PR URL has to look like to be stored in a field called `pr_url`.
# Host-agnostic on purpose (GitHub Enterprise, a proxy), but it must be a URL
# ending in `/pull/<n>` — which is the form `runs._outcome_detail` parses the
# number off, and the form `agent runs` renders.
_PR_URL_RE = re.compile(r"^https?://\S+/pull/\d+/?$")
_REMOTE_RE = re.compile(
    r"^(?:git@[^:]+:|(?:ssh|git|https?)://[^/]+/)(?P<slug>[^/]+/.+?)(?:\.git)?$"
)


def remote_slug(cwd: Path) -> str | None:
    """`owner/name` from this repo's `origin` remote, or None when unreadable.

    Read from git rather than from `gh`, so it costs no network round trip: its
    one caller is `normalise_pr_url`, which can be reached from a session-end
    hook running in a finishing agent's critical path.
    """
    proc = run(["git", "remote", "get-url", "origin"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    match = _REMOTE_RE.match(proc.stdout.strip())
    return match.group("slug") if match is not None else None


def normalise_pr_url(value: str, cwd: Path) -> str:
    """A `--pr` value as a real URL, or `CommandError` saying why it isn't one.

    `agent report --pr` is documented as taking a URL and was a bare `str`. An
    agent passed `--pr 121`, so the outcome record held `{"pr_url": "121"}` and
    `agent runs` — which parses the number off the end of the URL — rendered
    `PR #121 — 121` (issue #122). A number is what an agent will type, so it is
    accepted and expanded here rather than stored in a field named `_url` that
    is not one. Anything that is neither is rejected at this boundary, where
    the caller can still fix it, rather than after it is on disk.
    """
    text = value.strip().lstrip("#")
    if not text:
        raise CommandError("--pr was empty — pass the PR's URL, or just its number")
    if text.isdigit():
        slug = remote_slug(cwd)
        if slug is None:
            raise CommandError(
                f"--pr {text} is a PR number, and this repo's `origin` remote could not be "
                "read to expand it into a URL — pass the full URL instead"
            )
        return f"https://github.com/{slug}/pull/{text}"
    if _PR_URL_RE.match(text):
        return text.rstrip("/")
    raise CommandError(f"--pr {value!r} is neither a PR URL (…/pull/<number>) nor a PR number")


def get_issue(number: int, cwd: Path) -> dict[str, Any]:
    proc = run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body,labels,url,comments"],
        cwd=cwd,
    )
    return json.loads(proc.stdout)


def issue_view(repo: str, number: int) -> dict[str, Any]:
    """One issue's detail, for a repo that may not be the local checkout.

    `--repo` is spelled out rather than relying on a `cwd`'s remote, the same
    reasoning as `open_web_command` (`tui/data.py`): the TUI's selected issue
    can belong to any fleet repo, not just the one it was launched in — so
    this needs no `cwd` at all.
    """
    proc = run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,createdAt",
        ],
    )
    return json.loads(proc.stdout)


def create_pr(
    cwd: Path,
    *,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
    labels: Sequence[str] = (),
) -> str:
    cmd = ["gh", "pr", "create", "--base", base, "--title", title, "--body", body]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]
    proc = run(cmd, cwd=cwd)
    return proc.stdout.strip()


def pr_view(number: int, cwd: Path) -> dict[str, Any]:
    proc = run(
        ["gh", "pr", "view", str(number), "--json", "number,title,body,url,baseRefName"],
        cwd=cwd,
    )
    return json.loads(proc.stdout)


def pr_diff(number: int, cwd: Path) -> str:
    return run(["gh", "pr", "diff", str(number)], cwd=cwd).stdout


def comment_on_pr(number: int, body: str, cwd: Path) -> None:
    run(["gh", "pr", "comment", str(number), "--body", body], cwd=cwd)


def comment_on_issue(number: int, body: str, cwd: Path) -> None:
    run(["gh", "issue", "comment", str(number), "--body", body], cwd=cwd)


def pr_references_issue(pr: dict[str, Any], issue_number: int) -> bool:
    """True if this PR's branch matches or it will actually close the given issue.

    A bare `#N` mention in the title/body (e.g. cross-referencing a related but
    out-of-scope issue) does NOT count — only a real GitHub closing reference
    (`Fixes`/`Closes`/`Resolves` `#N`, as GitHub itself parses it, surfaced via
    `closingIssuesReferences`) or the conventional `fix/issue-N` branch name.
    """
    if pr.get("headRefName") == f"fix/issue-{issue_number}":
        return True
    closing_refs = pr.get("closingIssuesReferences") or []
    return any(ref.get("number") == issue_number for ref in closing_refs)


def open_pr_numbers(base: str, cwd: Path) -> list[int]:
    """Numbers of every open PR targeting `base`, ascending."""
    proc = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            base,
            "--json",
            "number",
            "--limit",
            "100",
        ],
        cwd=cwd,
    )
    prs: list[dict[str, Any]] = json.loads(proc.stdout)
    return sorted(pr["number"] for pr in prs)


def open_prs(cwd: Path) -> list[dict[str, Any]]:
    """Every open PR in this repo, with the fields `pr_references_issue` needs."""
    proc = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,body,headRefName,url,closingIssuesReferences",
            "--limit",
            "100",
        ],
        cwd=cwd,
    )
    return json.loads(proc.stdout)


def open_prs_for_issue(issue_number: int, cwd: Path) -> list[dict[str, Any]]:
    """Open PRs that already reference this issue, for the dedupe guard.

    Fails open (returns []) when `gh` can't answer — e.g. no remote in a
    test/scratch repo — since without a GitHub remote no duplicate PR can
    exist, and the run would fail later at push/PR anyway.
    """
    try:
        prs = open_prs(cwd)
    except CommandError:
        return []
    return [pr for pr in prs if pr_references_issue(pr, issue_number)]


@dataclass(frozen=True)
class Label:
    color: str
    description: str = ""


@dataclass(frozen=True)
class LabelSync:
    """What `sync_labels` did to each label it was given, by name."""

    created: list[str]
    updated: list[str]
    unchanged: list[str]
    failed: list[tuple[str, str]]


def why(stderr: str, stdout: str) -> str:
    """The one line of a failed `gh` invocation worth quoting."""
    for stream in (stderr, stdout):
        lines = [line for line in stream.strip().splitlines() if line.strip()]
        if lines:
            return lines[0]
    return "no output"


def sync_labels(
    project_root: Path, labels: Mapping[str, Label], *, repo: str | None = None
) -> LabelSync:
    """Create or update every label in `labels` to match its color/description.

    Idempotent: the repo's current labels are read once, so a repo already in
    sync makes no `gh label create` calls at all — this runs on every `init`
    and every triage/groom/scout invocation, and has to be silent when there
    is nothing to do. Each label is created with `check=False` so one failure
    (e.g. no write scope) is recorded and skipped rather than aborting the rest.

    `repo`, when given, is passed to `gh` as `--repo` on every call instead of
    letting `gh` resolve the repo from `cwd` itself: for a fork, that
    resolution lands on the upstream parent, not `origin` — silently writing
    labels to a repo nobody named. Pass the caller's own `remote_slug` here to
    pin it.
    """
    repo_args = ["--repo", repo] if repo else []
    proc = run(
        [
            "gh",
            "label",
            "list",
            "--json",
            "name,color,description",
            "--limit",
            "1000",
            *repo_args,
        ],
        cwd=project_root,
        check=False,
    )
    if proc.returncode != 0:
        raise CommandError(f"could not list labels: {why(proc.stderr, proc.stdout)}")
    try:
        entries = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"could not parse `gh label list` output: {exc}") from exc
    current = {
        entry["name"]: (
            entry["color"].lstrip("#").lower(),
            (entry.get("description") or "").strip(),
        )
        for entry in entries
    }

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    failed: list[tuple[str, str]] = []
    for name, label in labels.items():
        existing = current.get(name)
        wanted = (label.color.lstrip("#").lower(), label.description.strip())
        if existing == wanted:
            unchanged.append(name)
            continue
        create = run(
            [
                "gh",
                "label",
                "create",
                name,
                "--color",
                label.color,
                "--description",
                label.description,
                "--force",
                *repo_args,
            ],
            cwd=project_root,
            check=False,
        )
        if create.returncode != 0:
            failed.append((name, why(create.stderr, create.stdout)))
            continue
        (updated if existing is not None else created).append(name)
    return LabelSync(created=created, updated=updated, unchanged=unchanged, failed=failed)

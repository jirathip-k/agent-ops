from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult, wall_clock_timeout_result
from agent_ops.runtimes.credentials import environment_for
from agent_ops.utils import TIMEOUT_RETURNCODE, run

# Codex reports refusals as one JSON object per line on stderr, e.g.
# {"type":"error","status":400,"error":{"type":"invalid_request_error",
#  "message":"The 'fable' model is not supported when using Codex with a
#  ChatGPT account."}}
# A 400 alone says nothing about the model, so the message has to name one.
_MODEL_MARKERS = ("model", "engine")
_UNAVAILABLE_MARKERS = (
    "not supported",
    "not available",
    "does not exist",
    "unknown",
    "not found",
    "retired",
    "deprecated",
    "no access",
)
_TRANSIENT_STATUSES = (408, 409, 425, 429)
_PROVIDER_UNAVAILABLE_MARKERS = (
    "usage limit reached",
    "hit your usage limit",
    "quota exceeded",
    "insufficient_quota",
    "credits exhausted",
)

# Codex has no `--permission-mode`. It splits the same question in two: a
# sandbox policy (what a command may touch) and an approval policy (when it
# stops to ask). So the platform's mode — Claude Code's vocabulary, which is
# what config carries — is translated onto that pair rather than passed through.
#
# The translation is by *what a delegated worker experiences*, not by tool
# class, because under Codex an edit is just another shell command and the two
# cannot be split the way `acceptEdits` splits them:
#
#   never asks, writes confined to the workspace   → bypassPermissions
#   may ask when the model judges it worth asking  → acceptEdits, auto
#   asks before anything outside its trusted set   → default, manual
#   cannot write at all                            → plan, dontAsk
#
# `--dangerously-bypass-approvals-and-sandbox` is deliberately not a target for
# any mode: `bypassPermissions` needs a session that never stops to ask, which
# `--ask-for-approval never` gives on its own — dropping the sandbox too would
# hand a mode chosen for convenience a blast radius nothing in config asked for.
_APPROVAL_BY_MODE = {
    "bypassPermissions": ("workspace-write", "never"),
    "acceptEdits": ("workspace-write", "on-request"),
    "auto": ("workspace-write", "on-request"),
    "default": ("workspace-write", "untrusted"),
    "manual": ("workspace-write", "untrusted"),
    "plan": ("read-only", "never"),
    "dontAsk": ("read-only", "never"),
}


class CodexRuntime:
    """Headless OpenAI Codex via `codex exec`. Experimental — no session resume,
    no structured output parsing yet. Proves the runtime interface is real.
    """

    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, request: RunRequest) -> RunResult:
        prompt = request.prompt
        if request.system_prompt:
            prompt = f"{request.system_prompt}\n\n---\n\n{prompt}"

        cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check"]
        if request.model:
            cmd += ["--model", request.model]
        cmd.append(prompt)

        # `codex exec` has no streaming path (yet) to measure silence against,
        # so this call gets a wall-clock bound instead — the same one the
        # non-streaming path in claude_code.py uses.
        child_env = environment_for(self.name)
        env_kwargs: dict[str, Any] = {"env": child_env} if child_env is not None else {}
        proc = run(
            cmd,
            cwd=request.cwd,
            check=False,
            timeout=request.run_timeout_seconds,
            **env_kwargs,
        )
        if proc.returncode == TIMEOUT_RETURNCODE:
            return wall_clock_timeout_result(request.run_timeout_seconds)
        text = proc.stdout.strip() or proc.stderr.strip()
        # stderr is kept even when stdout wins the `text` slot: the error JSON
        # lands on stderr, and classification needs it.
        return RunResult(
            ok=proc.returncode == 0,
            text=text,
            raw={"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode},
        )

    def classify_failure(self, result: RunResult) -> FailureKind:
        return classify_failure(result)

    def interactive_command(
        self, prompt: str | None, *, permission_mode: str, model: str | None = None
    ) -> list[str]:
        cmd = ["codex", *approval_flags(permission_mode)]
        if model:
            cmd += ["--model", model]
        if prompt:
            cmd.append(prompt)
        return cmd

    def seed_stop_hook(self, worktree: Path, command: list[str]) -> Path | None:
        """None: Codex has no session-lifecycle hook to hang this on.

        Not a stub waiting to be filled in — until the CLI grows one, a Codex
        worker's completion is only ever inferred, so `agent spawn --runtime
        codex` says so at spawn time rather than letting a caller wait for a
        report that can never arrive.
        """
        return None


def approval_flags(permission_mode: str) -> list[str]:
    """`permission_mode` as the sandbox/approval pair Codex actually takes.

    Raises ValueError for a mode with no translation, rather than starting a
    session on whatever Codex defaults to: a spawned worker is not watched, so
    a policy nobody chose is a policy nobody would notice.
    """
    pair = _APPROVAL_BY_MODE.get(permission_mode)
    if pair is None:
        raise ValueError(
            f"codex has no equivalent for permission mode {permission_mode!r}; "
            f"expected one of: {', '.join(_APPROVAL_BY_MODE)}"
        )
    sandbox, approval = pair
    return ["--sandbox", sandbox, "--ask-for-approval", approval]


def classify_failure(result: RunResult) -> FailureKind:
    """Read Codex's JSON error envelope into a provider-neutral failure kind."""
    raw = result.raw or {}
    blobs = [result.text, str(raw.get("stderr", "")), str(raw.get("stdout", ""))]
    haystack = "\n".join(blobs).lower()
    if any(marker in haystack for marker in _PROVIDER_UNAVAILABLE_MARKERS):
        return FailureKind.PROVIDER_UNAVAILABLE
    for status, message in _error_envelopes(blobs):
        if status in _TRANSIENT_STATUSES or (status is not None and 500 <= status < 600):
            return FailureKind.TRANSIENT
        lowered = message.lower()
        if any(m in lowered for m in _MODEL_MARKERS) and any(
            m in lowered for m in _UNAVAILABLE_MARKERS
        ):
            return FailureKind.MODEL_UNAVAILABLE
    return FailureKind.AGENT_FAILURE


def _error_envelopes(blobs: list[str]) -> list[tuple[int | None, str]]:
    """Every `{"type":"error", ...}` object found across the CLI's output streams."""
    found: list[tuple[int | None, str]] = []
    for blob in blobs:
        for line in blob.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "error":
                continue
            status = event.get("status")
            error = event.get("error")
            message = error.get("message", "") if isinstance(error, dict) else str(error or "")
            found.append((status if isinstance(status, int) else None, str(message)))
    return found

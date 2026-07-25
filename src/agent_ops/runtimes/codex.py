from __future__ import annotations

import json
import shutil
from typing import Any

from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import run

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

        proc = run(cmd, cwd=request.cwd, check=False)
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


def classify_failure(result: RunResult) -> FailureKind:
    """Read Codex's JSON error envelope into a provider-neutral failure kind."""
    raw = result.raw or {}
    blobs = [result.text, str(raw.get("stderr", "")), str(raw.get("stdout", ""))]
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

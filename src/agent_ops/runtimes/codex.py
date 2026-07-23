from __future__ import annotations

import shutil

from agent_ops.runtimes.base import RunRequest, RunResult
from agent_ops.utils import run


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
        return RunResult(ok=proc.returncode == 0, text=text)

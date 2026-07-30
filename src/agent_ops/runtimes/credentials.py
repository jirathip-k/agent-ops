from __future__ import annotations

import os
from pathlib import Path

_CLAUDE_TOKEN_FILE_ENV = "AGENT_CLAUDE_CODE_OAUTH_TOKEN_FILE"
_CODEX_HOME_ENV = "AGENT_CODEX_HOME"

_runtime_env: dict[str, dict[str, str]] = {}


def capture_ci_credentials() -> None:
    """Capture CI runtime auth before target-controlled commands run.

    The hybrid lane writes the Claude token to a runner-temporary file and
    passes the proxy-backed Codex home under agent-ops-specific names. Consume
    both, remove their carrier variables from the process environment, and
    unlink the token file. Setup and gate subprocesses launched later therefore
    inherit neither provider's authentication.

    Local runs do not set either carrier and keep normal CLI-managed
    authentication unchanged.
    """
    token_file = os.environ.pop(_CLAUDE_TOKEN_FILE_ENV, None)
    if token_file is not None:
        path = Path(token_file)
        try:
            token = path.read_text().rstrip("\n")
        except OSError as exc:
            raise RuntimeError(
                f"Claude credential handoff failed: cannot read {path}: {exc}"
            ) from exc
        finally:
            path.unlink(missing_ok=True)
        if not token:
            raise RuntimeError(f"Claude credential handoff failed: {path} is empty")
        _runtime_env.setdefault("claude_code", {})["CLAUDE_CODE_OAUTH_TOKEN"] = token

    codex_home = os.environ.pop(_CODEX_HOME_ENV, None)
    if codex_home is not None:
        path = Path(codex_home)
        if not path.is_dir():
            raise RuntimeError(
                f"Codex proxy handoff failed: {path} does not exist; "
                "openai/codex-action must start the proxy first"
            )
        _runtime_env.setdefault("codex", {})["CODEX_HOME"] = str(path)


def environment_for(runtime: str) -> dict[str, str] | None:
    """A child environment with only this runtime's captured CI credentials."""
    overrides = _runtime_env.get(runtime)
    if not overrides:
        return None
    return {**os.environ, **overrides}

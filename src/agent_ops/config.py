from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_ops.utils import PLATFORM_ROOT

DEFAULTS_FILE = PLATFORM_ROOT / "config" / "defaults.yaml"
PROJECT_CONFIG_REL = Path(".agent") / "config.yaml"

ROLE_NAMES = ("planner", "implementer", "reviewer")


class ModelTierError(RuntimeError):
    """A role asked for a tier the effective runtime does not define.

    RuntimeError so the CLI's existing error handling reports it as a clean
    message; the alternative is handing a foreign model name to a CLI and
    letting it 400 halfway through a run.

    Carries the runtime/tier/role as fields as well as in the message, so a
    diagnostic can group gaps ("codex has no 'smart' for planner, reviewer")
    instead of re-parsing three near-identical sentences.
    """

    def __init__(self, message: str, *, runtime: str, tier: str, role: str) -> None:
        super().__init__(message)
        self.runtime = runtime
        self.tier = tier
        self.role = role


class Commands(BaseModel):
    setup: str | None = None  # run once in each fresh worktree before gates (e.g. npm install)
    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class LoopConfig(BaseModel):
    max_attempts: int = 3
    gates: list[str] = Field(default_factory=lambda: ["test", "lint", "typecheck"])
    # Wall-clock bound per gate command (and per `commands.setup` run). How long
    # a test suite takes is per-project, hence a knob; the default sits well
    # under the CI lane's 55-minute job timeout so a wedged gate still leaves
    # room for the run to report.
    gate_timeout_seconds: float = 1800.0
    # Streaming runs: kill (and reap) the child if it produces no output for
    # this long. Never a total-duration cap — a run still emitting events
    # keeps going no matter how long it has been running (issue #108).
    idle_timeout_seconds: float = 1200.0
    # Non-streaming runs: there is no stream to measure silence against, so
    # this is a real wall-clock bound instead — sized comfortably under the
    # CI lane's 55-minute job timeout (README.md) so a wedged run still fails
    # inside that window.
    run_timeout_seconds: float = 3000.0
    plan: bool = True
    self_review: bool = True
    auto_merge: bool = False  # after opening a PR, merge it into staging if merge rules pass


class RuntimeConfig(BaseModel):
    name: str = "claude_code"
    model: str | None = None
    permission_mode: str = "acceptEdits"
    # The mode for `agent spawn`'s interactive sessions, kept separate from the
    # headless one above because the two paths fail in opposite directions. A
    # headless run has nobody to ask, so an unapproved tool is *denied* and the
    # run carries on; an interactive one *waits*, and a delegated worker that
    # waits looks dead — its stop hook fires and reports it halted (issue #115).
    # Loosen or tighten per project here, or per spawn with `--permission-mode`.
    interactive_permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    stream: bool = True


class RoleConfig(BaseModel):
    """Per-role overrides; unset fields fall back to the project's runtime config."""

    runtime: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    max_turns: int | None = None


class AgentsConfig(BaseModel):
    planner: RoleConfig = Field(default_factory=RoleConfig)
    implementer: RoleConfig = Field(default_factory=RoleConfig)
    reviewer: RoleConfig = Field(default_factory=RoleConfig)


class ResolvedRole(BaseModel):
    runtime: str
    model: str | None
    permission_mode: str
    max_turns: int | None
    # Models to try, in order, only if `model` turns out to be unavailable.
    # Empty unless the project configures a ladder — no ladder, no change.
    fallbacks: list[str] = Field(default_factory=list)


class MergeConfig(BaseModel):
    """Rules for agent merges into the working branch (never the stable one)."""

    stable_branch: str = "main"  # promotion target; only humans merge into it
    max_changed_lines: int = 400
    max_changed_files: int = 12
    # Backstop multiplier applied to the caps above, counting ALL files
    # (including tests) — without it, excluding test lines from the production
    # caps would leave a mixed PR with no ceiling at all (200 production lines
    # + 50,000 test lines would auto-merge). Expressed as a ratio rather than
    # absolute numbers so it tracks a project's own overridden caps: a repo
    # that tightens max_changed_lines to 200 still gets a 4x backstop instead
    # of silently inheriting an 8x one.
    # ge=1: a ratio of 0 would zero both backstops and block every PR on a
    # single changed line. 1 is allowed but deliberate — it pins the total to
    # the production cap, opting the project out of the test exclusion.
    total_cap_ratio: int = Field(default=4, ge=1)
    # Test-file patterns excluded from max_changed_lines/max_changed_files
    # (see workflows.merge._is_test_file). Patterns anchored at a literal (not
    # already starting with `*`) need a `*/`-prefixed twin so they also match
    # nested copies — fnmatch's `*` spans `/`, so a pattern that already
    # starts with `*` (e.g. `*Tests/*`) already matches at any depth and needs
    # no twin. The `[!/]` in `test_[!/]*.py` / `*/test_[!/]*.py` anchors the
    # pattern to the filename so it can't swallow a whole subdirectory tree —
    # without it, `test_*.py` would match every `.py` file under a
    # `test_data/` directory (e.g. `test_data/schema.py`,
    # `src/test_data/schema.py`), both of which are production code despite
    # starting with `test_`.
    test_paths: list[str] = Field(
        default_factory=lambda: [
            "test_[!/]*.py",
            "*/test_[!/]*.py",
            "*_test.py",
            "tests/*",
            "*/tests/*",
            "__tests__/*",
            "*/__tests__/*",
            "*.test.*",
            "*.spec.*",
            "*_test.go",
            "*Tests.swift",
            "*Tests/*",
        ]
    )
    blocked_paths: list[str] = Field(
        default_factory=lambda: [
            ".github/*",
            "*auth*",
            "*migration*",
            "package.json",
            "package-lock.json",
            "requirements*.txt",
            "pyproject.toml",
            "uv.lock",
            "Dockerfile",
            "*.tf",
        ]
    )
    # A PR carrying one of these labels is a violation `evaluate_merge` raises
    # regardless of size/path — the actual enforcement behind labels like
    # `human-merge-only` (evolve.py), which `gh pr merge` does not itself
    # understand. Code-side default, same precedent as `blocked_paths`: a
    # project overrides it in `config/defaults.yaml`, not here.
    blocked_labels: list[str] = Field(default_factory=lambda: ["human-merge-only"])


class ReviewConfig(BaseModel):
    """Budget for the diff inlined into the reviewer prompt."""

    max_diff_lines: int = 5000


class DistillConfig(BaseModel):
    """Thresholds for `agent distill`'s AGENTS.md pruning pass."""

    min_lines: int = 200  # below this a run is a no-op — see run_distill
    # The six template headings (templates/project/AGENTS.md) are
    # human-authored by construction; anything else was appended by an agent
    # and is fair game to prune. Override per project if headings were renamed.
    protected_sections: list[str] = Field(
        default_factory=lambda: [
            "What this project is",
            "Architecture",
            "Conventions",
            "Commands",
            "Danger zones",
            "Maintaining this file",
        ]
    )


class ScoutConfig(BaseModel):
    """What this repo wants mined, on top of scout's standard signal list.

    Free text, written by the repo it scouts. The standard signals (TODOs,
    deferred review threads, swallowed errors, untested modules, stale docs)
    are near-empty on a static or marketing site, so scout files nothing
    there; `focus` is how such a repo names the signals that *do* exist for
    it. It names signals, never goals — "pages missing meta descriptions",
    not "do SEO research" — because a goal invites exactly the brainstorming
    scout is built to refuse.
    """

    focus: str = ""


class ProjectConfig(BaseModel):
    base_branch: str = "main"
    worktree_dir: str = ".worktrees"
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    # Per-runtime tier names → concrete models, e.g.
    # {"claude_code": {"smart": "fable", "fast": "sonnet"}}. A tier names a job
    # — `smart` for planning and review, `fast` for implementation — not a
    # vendor, so roles reference tiers and upgrading every role is a one-line
    # change. Keyed by runtime because what a model string means is the
    # runtime's business; a tier the effective runtime does not define raises
    # ModelTierError rather than reaching a CLI that cannot serve it.
    model_tiers: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Per-runtime fallback ladders, keyed by the same names as model_tiers:
    # {"claude_code": {"smart": ["fable", "opus"]}}. Used ONLY when a model
    # turns out to be unavailable mid-run (spend limit, unsupported, retired);
    # a run that never hits a limit never looks at this. Empty by default, so
    # the mechanism is inert until a project (or defaults.yaml) opts in.
    # See docs/workflow.md for how to refresh a ladder against the Models API.
    model_fallbacks: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    distill: DistillConfig = Field(default_factory=DistillConfig)
    commands: Commands = Field(default_factory=Commands)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    skills: list[str] = Field(default_factory=list)
    scout: ScoutConfig = Field(default_factory=ScoutConfig)

    def effective_runtime(self, role_name: str, runtime_override: str | None = None) -> str:
        """The runtime a role will actually run on: CLI override, role, then base."""
        role: RoleConfig = getattr(self.agents, role_name)
        return runtime_override or role.runtime or self.runtime.name

    def resolve_role(self, role_name: str, *, runtime_override: str | None = None) -> ResolvedRole:
        """Merge a role's overrides over the base runtime config, mapping model tiers.

        Tiers resolve against the *effective* runtime, so `--runtime codex`
        looks up `smart` in the codex table rather than handing codex a Claude
        model name.
        """
        role: RoleConfig = getattr(self.agents, role_name)
        runtime = self.effective_runtime(role_name, runtime_override)
        tiers = self.model_tiers.get(runtime, {})
        requested = role.model or self.runtime.model

        model = requested
        if requested is not None:
            if requested in tiers:
                model = tiers[requested]
            elif requested in self.tier_names():
                # A tier name every other runtime knows, missing here — passing
                # it through would just fail later inside the CLI.
                defined = ", ".join(sorted(tiers)) or "no tiers at all"
                raise ModelTierError(
                    f"runtime {runtime!r} has no model for tier {requested!r} "
                    f"(role {role_name!r}); model_tiers.{runtime} defines {defined}. "
                    f"Add the tier to model_tiers.{runtime} or pin a concrete model on the role.",
                    runtime=runtime,
                    tier=requested,
                    role=role_name,
                )

        return ResolvedRole(
            runtime=runtime,
            model=model,
            permission_mode=role.permission_mode or self.runtime.permission_mode,
            max_turns=role.max_turns if role.max_turns is not None else self.runtime.max_turns,
            fallbacks=self._fallbacks_for(runtime, requested, model),
        )

    def tier_names(self) -> set[str]:
        """Every name used as a tier by any runtime.

        A name only counts as a tier if some runtime declares it; anything else
        is a concrete model the user pinned and is passed through untouched.
        """
        return {tier for tiers in self.model_tiers.values() for tier in tiers}

    def _fallbacks_for(self, runtime: str, requested: str | None, model: str | None) -> list[str]:
        """The rungs below `model` in this runtime's ladder for `requested`.

        The configured ladder is the full sequence (e.g. smart: [fable, opus]),
        so the active model and anything above it are dropped — a ladder can
        only ever step down from where the role already is.
        """
        if requested is None:
            return []
        tiers = self.model_tiers.get(runtime, {})
        rungs = [
            tiers.get(rung, rung)
            for rung in self.model_fallbacks.get(runtime, {}).get(requested, [])
        ]
        if model in rungs:
            rungs = rungs[rungs.index(model) + 1 :]
        return [rung for rung in dict.fromkeys(rungs) if rung != model]


@dataclass(frozen=True)
class RoleReport:
    """What one role would run with — or why it cannot run at all."""

    name: str
    runtime: str
    model: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    error: str | None = None
    # Set only when `error` is a missing tier: the tier that has no entry.
    missing_tier: str | None = None


@dataclass(frozen=True)
class RuntimeReport:
    """What every role would run with if this runtime were the one in effect."""

    runtime: str
    roles: list[RoleReport]

    def missing_tiers(self) -> dict[str, list[str]]:
        """Tier → the roles that would be refused for want of it, in role order."""
        gaps: dict[str, list[str]] = {}
        for role in self.roles:
            if role.missing_tier is not None:
                gaps.setdefault(role.missing_tier, []).append(role.name)
        return gaps


def role_reports(config: ProjectConfig, *, runtime: str | None = None) -> list[RoleReport]:
    """Resolve every role for `agent doctor`, turning failures into report rows.

    A misconfigured ladder or a tier the runtime does not define should be
    visible before a run dies on it, so resolution errors are reported per role
    instead of aborting the whole check.

    `runtime` resolves as though `--runtime <name>` had been passed, so the
    same code answers both "what will this project run" and "what would this
    project run on that other runtime".
    """
    reports: list[RoleReport] = []
    for name in ROLE_NAMES:
        effective = config.effective_runtime(name, runtime)
        try:
            resolved = config.resolve_role(name, runtime_override=runtime)
        except ModelTierError as exc:
            reports.append(
                RoleReport(name=name, runtime=effective, error=str(exc), missing_tier=exc.tier)
            )
            continue
        reports.append(
            RoleReport(
                name=name,
                runtime=resolved.runtime,
                model=resolved.model,
                fallbacks=resolved.fallbacks,
            )
        )
    return reports


def runtime_reports(config: ProjectConfig, runtimes: Iterable[str]) -> list[RuntimeReport]:
    """`role_reports` for each of `runtimes`, as if each were the one in effect.

    A runtime with no tier table refuses at resolution rather than handing a
    foreign model name to a CLI — which is the right behaviour, and useless if
    the only way to discover it is to start a run. This is what lets `agent
    doctor` say so first.

    The runtime names are passed in rather than read from `agent_ops.runtimes`:
    config stays a leaf module that knows tier tables are keyed by runtime and
    nothing whatsoever about which runtimes exist.
    """
    return [
        RuntimeReport(runtime=name, roles=role_reports(config, runtime=name)) for name in runtimes
    ]


def ladder_warnings(config: ProjectConfig) -> list[str]:
    """Ways a `model_fallbacks` table disagrees with the `model_tiers` beside it.

    Neither case is an error: a ladder is inert until a model goes unavailable.
    That is exactly the problem — both stay invisible until the day they matter,
    which is a day a run is already failing. See docs/workflow.md.
    """
    warnings: list[str] = []
    tier_names = config.tier_names()
    for runtime in sorted(config.model_fallbacks):
        tiers = config.model_tiers.get(runtime, {})
        for tier, rungs in sorted(config.model_fallbacks[runtime].items()):
            if not rungs:
                continue  # an emptied ladder is how a project opts out
            if tier not in tiers:
                if tier in tier_names:
                    warnings.append(
                        f"model_fallbacks.{runtime}.{tier} is a ladder for a tier "
                        f"model_tiers.{runtime} does not define, so nothing can reach it"
                    )
                continue
            model = tiers[tier]
            resolved = [tiers.get(rung, rung) for rung in rungs]
            if model not in resolved:
                warnings.append(
                    f"model_tiers.{runtime}.{tier} is {model!r}, which is not on "
                    f"model_fallbacks.{runtime}.{tier} ({' → '.join(resolved)}). The ladder is "
                    f"trimmed below the active model only when the active model is on it, so an "
                    f"availability failure would step UP into {resolved[0]!r}"
                )
    return warnings


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_project_config(project_root: Path) -> ProjectConfig:
    """Platform defaults merged with the project's .agent/config.yaml (project wins)."""
    defaults = _load_yaml(DEFAULTS_FILE)
    project = _load_yaml(project_root / PROJECT_CONFIG_REL)
    return ProjectConfig.model_validate(_deep_merge(defaults, project))

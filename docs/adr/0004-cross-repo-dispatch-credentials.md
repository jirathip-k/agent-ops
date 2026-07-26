# ADR 0004: Central dispatch runs in agent-ops on a per-target App token

**Status:** proposed · 2026-07-26

## Context

All six reusable pipelines already declare `workflow_dispatch` with a
`target_repo` input, so agent-ops' own Actions tab already has the six
buttons. What does not work is auth. Every step that reaches the target
authenticates with `secrets.GITHUB_TOKEN` — the target checkout
(`spec-pipeline.yml:124`, `groom:79`, `plan:123`, `scout:69`, `promote:60`)
and every `gh` call (`spec:70,141`, `groom:58,96`, `plan:69,140`,
`scout:83`, `promote:88`, `triage:91`). In a run hosted by agent-ops that
token is scoped to agent-ops, so a dispatch against another repo 404s on
checkout and fails every issue write.

The alternative — teach each managed repo's stub to accept dispatch inputs —
costs a PR to every managed repo, which is the churn #96 exists to remove,
and `stubs.py:22` compares only `("secrets", "permissions")`, so a repo left
un-PR'd would be silently stale with no drift warning.

Central dispatch needs a target list, and `config/repos.yml:1-5` records
that no private repo name enters this public repo. `registry.py:12` keeps
the real list in git-ignored `config/local/repos.yml`, which CI cannot read.

## Decision

**1. agent-ops dispatches; managed repos keep their schedules.** The
`workflow_dispatch` triggers already on the pipelines become the manual
surface for every repo. Stubs are untouched; their `on: schedule:` runs
continue to execute in the managed repo as today.

**2. The App token is narrow.** It replaces `GITHUB_TOKEN` only where the
step genuinely crosses a repo boundary: the target checkout, the `GH_TOKEN`
env of every step shelling out to `gh` against `target_repo`, and the
`github_token` input of the triage lane's `claude-code-action` step
(`triage-pipeline.yml:196`), whose orchestrator makes that lane's entire
cross-repo reach. Everything else keeps `GITHUB_TOKEN`: the control-repo
checkout needs no token, and each job's `permissions:` block now governs a
token that only ever touches agent-ops.

**3. The App gains Issues and Pull requests write.** Today it is Contents:
R/W + Metadata: Read (`docs/ci-cd.md:107-113`). Central dispatch adds
**Issues: Read & write** (labels, comments, `issue edit` — every lane but
promote) and **Pull requests: Read & write** (promote opens the promotion
PR; the triage orchestrator opens and merges). **Checks: Read** goes in the
same increase, for the auto-merge gate's check rollup — not because it is
proven necessary, but because a permission *increase* must be re-accepted on
every owner while a reduction applies immediately (`docs/ci-cd.md:111-113`),
so guessing high costs one settings visit and guessing low costs a second
round of them. Actions: Read is not included; that is #95's need, not this
one.

**4. A missing installation fails the dispatch loudly.** Where
`inputs.target_repo != github.repository`, the mint step drops
`continue-on-error` and the run stops there. The existing fallback
(`triage-pipeline.yml:161,186`) is correct for a managed-repo-hosted run,
where `GITHUB_TOKEN` still reaches its own repo and only the push identity
degrades (#62) — it is worthless here, where the fallback token cannot reach
the target at all and merely defers the same failure to a confusing 404 on a
repo that plainly exists. The condition, not the lane, decides.

**5. The registry is a repo secret, `AGENT_MANAGED_REPOS`.** A repository
*variable* is unmasked and this repo's run logs are public, so private repo
names would land in them; a secret is masked. `config/repos.yml:1-5` is
therefore preserved, not reversed, and `config/local/repos.yml` stays as the
list local commands read.

**6. A bad entry is caught by a preflight, not by inspection.** Nothing can
diff the secret against `config/local/repos.yml` — `gh secret list` returns
names, not values — so the CI list has to report on itself. A preflight step
walks the roster before any lane work: shape-check each entry against
`owner/repo`, then mint a per-target token and `gh api repos/<repo>`.
Failures are reported by *index*, since the values are masked, and the two
outcomes discriminate cleanly: a mint failure means the App is not installed
or not granted on that owner, a successful mint plus a 404 means the entry
names no repo. The same preflight runs on a schedule, so an entry that goes
stale — a repo renamed or transferred — surfaces as a failed run rather than
waiting for someone to press dispatch. Detection, not automation: the shape
`docs/ci-cd.md:60-64` already uses for shared-ledger drift.

## Consequences

- **Blast radius stays per-repo.** Tokens are minted per target
  (`create-github-app-token` with `owner` + a single `repositories` entry,
  `triage-pipeline.yml:162-167`) and expire in an hour. A leaked one can
  push, open and merge PRs, and edit issues in **one** managed repo for that
  hour; branch protection on `main` is the remaining backstop. It cannot
  reach a second managed repo in the same run, a repo the installation was
  never granted, or any repo's Actions, secrets or settings. The crown jewel
  remains `AGENT_APP_PRIVATE_KEY`, which mints for everything the App is
  installed on — central dispatch adds no new copy of it, since agent-ops
  already holds it. Dispatch itself is collaborator-only: `workflow_dispatch`
  on a public repo requires write access.
- **A permission increase blocks rollout.** Until the installation is
  re-accepted on `jirathip-k`, `sendmeter` and
  `synergy-services-cooling-tower`, dispatch fails at the mint step for the
  owners not yet done — loudly, per decision 4, which is the intended
  mid-rollout behaviour.
- **`agent doctor` cannot drift-check the registry**, and no design makes it
  able to: the secret is unreadable from outside CI. It can only harden the
  mirror — today a typo'd entry in `config/local/repos.yml` renders as
  `⚠ no agent-ops lanes wired` (`status.py:173`), indistinguishable from a
  real repo with no lanes, because `_repo_workflows` swallows the 404
  (`status.py:117-118`). Separating "repo not found" from "no lanes" is worth
  doing, but it checks the local list, not the one CI runs on.
- **Two lists now exist** where one did. They drift by construction; the
  scheduled preflight is what makes the CI one honest, and nothing makes the
  local one honest but running `agent status`.

## Rejected alternatives

- **Dispatch inputs on the stubs (the narrow fix).** A PR to every managed
  repo, and `stubs.py:22` would not report the repos that never got one. It
  folds in later as a simplification, not as a substitute.
- **Making agent-ops private and putting the real list in
  `config/repos.yml`.** The managed repos span three owners and every one
  calls `uses: jirathip-k/agent-ops/.github/workflows/<lane>-pipeline.yml@main`.
  A private repo shares reusable workflows only within its own owner, so this
  breaks all six lanes for the five repos outside `jirathip-k`.
- **One installation token replacing `GITHUB_TOKEN` throughout all six
  pipelines.** Fewer moving parts, one credential able to write to every
  managed repo for the length of every run. The complexity of decision 2 is
  the price of not having that.
- **A fine-grained PAT.** Ruled out already (`docs/ci-cd.md:82-84`).

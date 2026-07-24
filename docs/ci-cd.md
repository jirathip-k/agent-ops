# CI/CD with agents, databases, and Supabase

Cross-repo doctrine for how code, edge functions, and database changes get
from an agent's branch to production. The concrete implementations live in
the managed repos; this records the rules and why they exist.

## Principles

1. **Humans own main.** Agents work on branches; merge requires the gates
   (and, where configured, human review). Nothing in this doc changes that.
2. **Merge is the only deploy trigger.** If deploying requires remembering
   a manual step, the deployed state will silently drift from the merged
   state — the motivating example is synergy-costing#120, where merged edge
   function code was not live anywhere and CI was green. Every deployable
   artifact needs a workflow that fires on merge, or a scheduled check that
   screams when reality and the repo disagree.
3. **Agents never deploy by hand.** An agent that "helpfully" runs
   `functions deploy` or `db push` outside CI bypasses the audit trail and
   the environment mapping. Agents change files; pipelines change
   environments.
4. **A failed deploy must fail loudly.** A red workflow is recoverable;
   silent drift is not.

## Branch → environment mapping

All managed repos use the same two-lane mapping:

| Branch    | Environment | Supabase target |
|-----------|-------------|-----------------|
| `staging` | dev/staging | dev project     |
| `main`    | production  | prod project    |

Every deployable (frontend, edge functions, migrations) follows this same
mapping in the same repo — no artifact gets its own bespoke branch scheme.

## Frontends and edge functions

- Frontends deploy on merge via the platform integration (Azure Static Web
  Apps or Vercel). Nothing agent-specific here.
- Edge functions deploy on merge via a workflow (`push` to `staging`/`main`,
  path-filtered to `supabase/functions/**` + `supabase/config.toml`).
  **Deploy all functions, not a changed-files subset**: deploys are
  idempotent and cheap, and a diff-based matrix mis-detects shared-module
  edits (a `_shared/` change must redeploy every importer). See
  synergy-costing#120.
- Function *secrets* are stored per-project in Supabase and are untouched
  by `functions deploy` — the dev/prod secret split survives deploys.

## Databases: the ledger-ownership rule

Whether migrations may be auto-applied depends on one question: **does this
repo own its `schema_migrations` ledger alone?**

- **Solo ledger** (one repo, its own dev + prod projects): automate.
  `supabase db push` on merge, staging → dev, main → prod. Push is
  idempotent and the ledger is authoritative. Example: climbing-tracker#130.
- **Shared ledger** (multiple repos writing into the same projects): never
  auto-push. `db push` silently skips any version it believes is applied —
  on a shared ledger that is schema-drift-with-no-error (synergy-costing#72).
  Instead: (a) a naming gate that partitions the version namespace per repo
  (synergy-inspection reserves the seconds field "30"), and (b) a
  **scheduled drift check** that compares repo migrations against the live
  ledgers and fails loudly — detection, not automation. Example:
  synergy-inspection#42.

Prod schema changes on shared-ledger repos follow the documented manual
checklist (apply + verify advisors/policies/objects) — a human-paced flow
with verification steps that do not belong in a fire-and-forget job.

## Agent access to databases (MCP scoping)

- Each Supabase-backed repo declares its MCP servers in a checked-in
  `.mcp.json`: `supabase-dev` (writable, pinned to the dev `project_ref`)
  and, where useful, `supabase-prod` with `read_only=true`. The hosted
  OAuth server (`mcp.supabase.com`) means no tokens on disk.
- Global/user scope gets at most a `read_only=true` server for cross-project
  browsing. **Write access exists only inside the repo that owns the
  project.**
- Prod is read-only via MCP everywhere. An agent may inspect prod to debug;
  the only write paths to prod are CI (solo ledger) or the human checklist
  (shared ledger).
- No personal access tokens in plaintext config. CI uses repo secrets
  (`SUPABASE_ACCESS_TOKEN`); humans use OAuth. A PAT that has ever sat in a
  config file on disk gets revoked, not reused.

## Secrets and approval gates

- Deploy credentials live in GitHub repo secrets, scoped through GitHub
  *environments* (`preview`/`production`) so prod jobs can grow an approval
  gate without restructuring the workflow.
- Per-environment app secrets (API keys, tenant overrides) live in the
  target platform (Supabase function secrets, SWA/Vercel env vars), never
  in the repo.

## Checklist for onboarding a repo

1. Branches: `staging` + `main`, mapped as above.
2. `.mcp.json`: dev writable, prod read-only, committed.
3. Frontend CD wired to the platform integration.
4. Edge functions present? Add the deploy-all-on-merge workflow.
5. Ledger solo or shared? Solo → `db push` CD; shared → naming gate +
   scheduled drift check.
6. `SUPABASE_ACCESS_TOKEN` as a repo secret via environments; no PATs on
   disk anywhere.
7. CLAUDE.md states what deploys automatically and what remains manual, so
   agents don't improvise.

## Moving or renaming a managed repo

Runbook for transferring a repo to another owner (e.g. personal → its own
org) and optionally renaming it. First exercised for
jirathip-k/climbing-tracker → sendmeter/sendmeter (2026-07-24).

### What survives a transfer

Issues, PRs, labels, releases, branch protections, **repo-level** Actions
secrets/variables/environments, and Actions run history all move with the
repo. GitHub creates redirects for git and web URLs — valid until someone
claims the old name. **Org-level** secrets do NOT follow the repo; anything
the old owner provided at org/user level must be re-created in the new org.
The scheduled triage workflow keeps firing (schedules attach to the repo's
default branch), and `uses: <owner>/agent-ops/...` references keep working
because agent-ops is public and does not move.

### Order of operations

1. Merge or close in-flight PRs first (they survive, but it's tidier).
2. Transfer: repo Settings → Danger Zone → Transfer ownership → target
   org. As an owner of both, it completes immediately.
3. Rename (optional): new repo Settings → General → Rename. Transfer and
   rename are independent; both leave redirects.

### Re-add checklist

1. **Local clone**: `git remote set-url origin
   git@github.com:<org>/<name>.git` — once per clone; linked worktrees
   share the same remote. Keep the local folder path unless you enjoy
   re-adding the repo to every tool that keys on it.
2. **Orca**: repo identity derives from the origin remote + folder path.
   After `set-url`, verify with `orca repo show`; only if the folder was
   also renamed, re-add via `orca repo add --path <new-path>`.
3. **Secrets**: `gh secret list -R <org>/<name>` — verify
   `CLAUDE_CODE_OAUTH_TOKEN` (and any deploy secrets) survived; re-add
   anything that was org/user-level at the old owner. Create pending
   GitHub environments (e.g. `testflight`) in the new location, not the
   old one.
4. **GitHub Apps do NOT follow the repo** — every app the pipeline or
   platform relies on must be installed on the new org:
   - **Claude Code app** (github.com/apps/claude) — without it the triage
     pipeline fails with "Claude Code is not installed on this repository"
     even though the OAuth token secret transferred fine.
   - **Vercel app**, then relink the project to the new repo slug
     (`vercel git connect` fails with a generic "make sure you have
     access" error until the app is installed). Platform-side env vars
     are untouched.
   - Any runner provider's app (e.g. Blacksmith), subject to its own
     account screening.
5. **Actions policy**: confirm the org allows Actions and public reusable
   workflows. Billing note: minutes now draw from the new org's plan
   (free org = 2,000 min/mo, macOS at 10×).
6. **In-repo self-references**: update CLAUDE.md / README links to the
   old slug (redirects mask this; fix it anyway).
7. **Cross-repo references**: agent-ops docs and memory that name the old
   slug still resolve via redirects — update opportunistically.

### Verify

`gh repo view <org>/<name>` → correct slug; `git fetch` still works from
an un-updated clone (redirect); `workflow_dispatch` the triage workflow
once; next merge deploys on the platform integration.

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

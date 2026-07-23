# Fleet status — agent-ops managed repos (TEMPLATE)

> Copy to `config/local/fleet.md` (git-ignored) and fill in with your real
> repos — the local copy is the living document, kept out of version control
> so private repo names never enter this public repo. Publish it as a private
> artifact for easy viewing; update + republish when the fleet changes.

## Summary table

| Repo | Visibility | Default | Staging model | Gates | Scheduled triage | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| you/platform-repo | public | main | — (control repo) | tests · lint · types | ✅ self, report-only | main ruleset: no force-push/delete |
| you/app-repo | private | **staging** | ✅ + auto-merge | test · lint · typecheck | ✅ live | |
| you/simple-repo | public | main | — main-only | ⚠️ none | stub committed | every PR human-merged |

## Rules in force everywhere

- Agents merge only into `staging` (where it exists) under rules: CI green,
  diff caps, no blocked paths (deps, auth, migrations, CI, infra).
- `main` is human-only. Promotions via `agent promote` PR, merged with a
  **merge commit**, staging branch kept.

## GitHub Actions inventory (which workflow does what, where)

The agent pipeline is ONE workflow run per repo per tick — each repo's stub
(`triage.yml`) calls the shared reusable pipeline (`triage-pipeline.yml` in
this repo), which runs one claude-code-action session that spawns the crew
(planner → implementer → tester → reviewer) as sub-agents inside itself.

| Repo | Workflow | Purpose | Trigger |
| --- | --- | --- | --- |
| platform-repo | `ci.yml` | platform checks | push/PR |
| platform-repo | `triage-pipeline.yml` | shared reusable pipeline | workflow_call |
| each managed repo | `triage.yml` | agent loop stub | cron + manual |
| (app repos) | deploy/CI workflows | pre-existing app automation | per app |

Manual trigger: Actions tab → Run workflow · `gh workflow run triage.yml
--repo <r>` · gh-dash: `t` on any row.

## Open items

- [ ] (keep a running human/crew checklist here)

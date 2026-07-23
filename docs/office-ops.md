# Office automation (email, Teams) — separate repo suggestion

Non-dev automation belongs in its own repo (`office-ops`), not here. The SDLC
platform's core loop is *verify with deterministic gates, retry, merge* —
none of that applies to email and Teams, where actions are irreversible sends
and the "evaluator" is your judgment. Different trust model, different repo.

## Recommended shape

```
office-ops/
├── CLAUDE.md            # who you are, tone, languages (EN/TH), signature rules
├── skills/
│   ├── inbox-triage.md  # classification rules: urgent / respond / archive
│   ├── email-drafting.md# tone per audience, must-never-send rules
│   └── teams-digest.md  # what counts as signal in team chats
├── routines/            # one prompt file per scheduled job
│   ├── morning-brief.md # summarize overnight email + Teams, propose replies
│   └── weekly-review.md
└── memory/              # rolling context: projects, people, commitments
```

## How it runs

- **Interactive**: open Claude Code in `office-ops` with the Microsoft 365
  MCP connector (Outlook mail/calendar + Teams tools) and ask for triage or
  drafts. The connector is already available in your Claude session.
- **Scheduled**: Claude Code's `/schedule` (cloud routines) or a local
  `/loop` for things like a 7:30 morning brief. Start with the brief only.

## Safety rules (the equivalent of "humans own main")

1. **Draft, never send.** Agents create drafts (`outlook_create_draft`,
   reply drafts); you press send. Never grant unattended send in v1.
2. No deletion — archive/label only, and only for high-confidence categories
   (newsletters, automated notifications).
3. Calendar: propose slots, never accept/decline meetings autonomously.
4. Keep a `must-never` list in CLAUDE.md (e.g. never auto-reply to your
   manager, HR, or external clients).

Graduate autonomy the same way as the dev pipeline: report-only week one,
then drafts, then auto-archive of the safest categories.

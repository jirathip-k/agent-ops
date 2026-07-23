# Review

- Read the issue/PR description first; judge the diff against its stated
  intent, not against how you would have written it.
- Trace every changed conditional: what input makes the new branch wrong?
- Treat touches to auth, CI, migrations, and dependency files as findings by
  default — they need explicit justification.
- Missing tests for changed behavior is a REQUEST CHANGES, not a suggestion.
- Every finding needs file:line and a concrete fix. "This could be cleaner"
  is not a finding.

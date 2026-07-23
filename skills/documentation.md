# Documentation

- Update docs in the same change as the code they describe — README examples,
  CLI help text, and config references must not drift.
- Write comments only for the why (constraints, invariants, non-obvious
  decisions), never the what.
- Prefer runnable examples over prose descriptions.
- If the change alters a public interface (CLI flag, config key, API), grep
  the docs for the old name before finishing.

# Testing

- Every behavior change ships with a test that fails without the change.
  Write the test first when practical; at minimum, verify it fails when you
  revert your fix.
- Test the contract, not the implementation: assert on outputs and effects,
  not internal call sequences.
- Cover the edge that caused the bug, not just the happy path.
- Keep tests fast and deterministic: no network, no sleeps, no shared state.
  Use tmp directories and fixtures.
- Run the project's full test command before declaring done — not just the
  tests you added.

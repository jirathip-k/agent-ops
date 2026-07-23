# Coding

- Read neighboring code before writing: match the file's naming, error
  handling, and comment density. New code should be indistinguishable in style
  from what surrounds it.
- Prefer editing an existing function over adding a parallel one.
- No dead code, no commented-out code, no TODOs for work you can do now.
- Handle the failure path first: what happens on bad input, missing file,
  non-zero exit? An unhandled edge case is a bug you shipped.
- If you find an unrelated bug, note it in your summary — do not fix it.

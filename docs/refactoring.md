# Refactoring policy

Do not maintain backward compatibility for removed project-owned behavior or
data shapes. Prefer deletion and a unified current model:

- Remove code that is not needed.
- Refactor code that is needed and can be improved.
- Keep code as-is only when it is needed and cannot be improved safely within
  the task.
- Never use a local patchwork compatibility layer when one unified model is
  possible.
- Do not pre-design compatibility machinery for hypothetical future forks.
  Model only the current canonical shape.
- Do not invent project-owned schema, format, materializer, converter, or
  implementation version numbers before a concrete compatibility boundary
  exists.
- Do not hide compatibility identity in variables, constants, cache-key salts,
  or other hard-coded markers. Externally required protocol and file-format
  version fields remain valid.
- If a real hard fork becomes necessary, add an explicit persisted or exchanged
  field together with the fork behavior at that time. Do not reserve fields in
  advance.
- For files larger than 1000 lines, consider splitting them.
- Use subdirectories or deeper module trees when they improve responsibility
  boundaries.
- Define responsibility boundaries and invariants before a refactor. Reject a
  split whose boundary or invariant cannot be stated clearly.

# Package import and export

## Common boundary

Package archives are untrusted input. Importers MUST reject unsafe member paths,
links or members that escape extraction, unsupported shapes, and configured size
or count limit violations. Import writes canonical workspace source; an imported
archive is not retained as the source of truth after conversion.

The application currently imports Native, Polygon, and ICPC packages. Their
converters map accepted external files into the current workspace layout and
report validation errors at the archive boundary.

## Native materialization

Native materialization is built from a specific published source commit and a
successful compatible verification. It contains the canonical manifest,
committed source, and materialized testcase data in manifest order. The durable
row records source commit, digest, revision, verification, archive locator, SHA,
size, and current availability.

Readiness, Git provenance, and archive availability are separate facts:
successful verification does not itself prove that an old archive still exists,
and an available archive always retains the source commit from which it was
built.

## ICPC export

ICPC export produces one hybrid ZIP; there are no selectable compatibility
profiles. The archive contains:

- `problem.yaml`
- `domjudge-problem.ini`
- the current `statement/` tree
- the legacy `problem_statement/` mirror

Representable pass-fail, interactive, and multi-pass problems use
`problem_format_version: 2025-09`. Interactive multi-pass falls back to
`problem_format_version: legacy`, because that combination is not represented
by the 2025-09 mode field used by the exporter.

Every rendered statement-language PDF is exported and mirrored into the legacy
directory. English is preferred for `problem_statement/problem.pdf`; when it is
absent, the first exported language is used.

The output aims at PPF 2025-09 and best-effort legacy DOMjudge consumption.
There is no compatibility release gate and no guarantee for every legacy
DOMjudge version. The export cache may reuse an available archive for the same
materialization/type/options identity. If rebuilding is required, ZIP bytes,
timestamps, ordering effects, and SHA are not guaranteed to match an earlier
build.

## Polygon import

Polygon import accepts the supported Polygon archive layout, converts tests,
programs, configuration, and statements into the canonical workspace, and
preserves usable statement assets. Unsupported or ambiguous input is rejected
rather than stored as a parallel legacy source model.

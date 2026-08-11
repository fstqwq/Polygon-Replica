# Package import and export

## Common boundary

Package archives are untrusted input. Uploads are copied to a cache temporary
file in chunks. `UPLOAD_MAX_BYTES` limits compressed upload bytes; it does not
stand in for an expansion limit.

Before constructing the standard-library ZIP member collection, the importer
parses EOCD/ZIP64 records, rejects multi-disk or out-of-bounds central
directories, streams the central directory, and enforces the real record count.
Every member, including directories and ignored members, counts toward the
entry ceiling and undergoes normalized-path, duplicate/conflict, traversal, and
central-directory integrity checks. A problem ZIP permits at most 4096 entries.

Only members selected by the active format importer are opened. Selected
members MUST be regular, unencrypted content and MUST NOT be symlinks or special
files. Their declared uncompressed sizes are checked before opening and actual
decompressor output is charged again while streaming to an isolated staging
tree. `PROBLEM_ZIP_MAX_EXPANDED_BYTES` controls this consumed-byte budget; its
default is 256 MiB and its configured range is 64 MiB through 4 GiB. Unknown or
unconsumed members still count as entries but do not spend expanded-byte budget.
ZIP64 is accepted within these limits; no compression-ratio rule exists.

Each parsed XML, YAML, INI, or JSON document and the retained metadata set are
bounded by the fixed 4 MiB metadata limit. Import writes canonical workspace
source; an imported archive is not retained as source of truth after conversion.

The application currently detects Native packages by `config/problem.json`,
Polygon packages by `problem.xml`, and ICPC packages by `problem.yaml`. Their
converters map accepted external files into the current workspace layout and
report validation errors at the archive boundary. Detection checks Polygon,
then ICPC, then Native when an archive contains more than one marker.

Importing as a new problem converts into an unborn workspace, commits the
result, and pushes `main`. Importing into an existing problem converts in a
staging directory, overwrites paths present in the converted tree, and keeps
existing paths absent from that tree; it leaves those merged changes uncommitted.

Native import copies the allowed authored workspace roots and discards packaged
`test_data/`; its manifest payload is derived data, not authored source. ICPC
import accepts an absent format version plus `legacy`, `legacy-icpc`, and
`2025-09`, with `pass-fail`, `interactive`, and `multi-pass` type tokens.

Agent workspace compare/apply uses the same file-backed archive admission and
consumed-byte accounting. Its selected files are streamed into a temporary
workspace tree before comparison or replacement.

## Native materialization

Native materialization is built from a specific published source commit and a
successful full verification of that snapshot. It contains the canonical
manifest, committed source, and materialized testcase data in manifest order.
The durable row records source commit, digest, revision, verification, archive
locator, SHA, size, and current availability.

The archive keeps committed source at its root and adds
`test_data/manifest.json` plus `test_data/tests/<id>/...`. The manifest has the
exact top-level fields `source_commit`, `revision_number`, `source_digest`,
`mode`, `pass_limit`, `verification`, and ordered `tests`. Each test records
`id`, `kind`, `sample`, an `input` descriptor, and optional `answer`,
`sample_input`, and `sample_output` descriptors. A descriptor contains only
`path`, `sha256`, and `size`. Non-interactive tests require answers; sample
overrides are stored only when they differ from judge data.

Reading a Native archive rechecks its stored size and SHA, safe member types,
manifest identity, every declared payload, the absence of undeclared testcase
payloads, and the committed-source digest. Failed validation marks the
materialization unavailable and invalidates its derived exports.

Readiness, Git provenance, and archive availability are separate facts:
successful verification does not itself prove that an old archive still exists,
and an available archive always retains the source commit from which it was
built.

On Native import, `test_data/**` is materialized judging output rather than Git
source. Those members, including `manifest.json`, test inputs, answers, and
sample overrides, are never opened, decompressed, or copied. They still count
toward the 4096-entry ceiling. Native import restores only the Git source shape.

## Contest import

`CONTEST_MAX_PROBLEMS` is an admission limit with default 26 and configured
range 1-64. The outer Contest ZIP receives derived limits:

```text
entry limit    = CONTEST_MAX_PROBLEMS x 4096
expanded limit = CONTEST_MAX_PROBLEMS x PROBLEM_ZIP_MAX_EXPANDED_BYTES
```

Every child also independently satisfies the single-problem entry and expanded
limits. Review retains the original bounded ZIP draft plus bounded display
metadata. Confirm reopens that draft and imports one child archive view at a
time; it does not materialize all child ZIP payloads in memory. The configured
problem count is checked before a Contest row is created. Manual additions use
the same limit, with count, next position, and insert serialized in one SQLite
writer transaction. Lowering the limit does not disable reading, editing,
building, exporting, or deleting an already over-limit Contest; it only rejects
new roster entries.

Only a terminal `ok` verification with its once-installed complete task graph is
eligible for materialization. A queued, running, failed, or pre-activation
verification is not ready. Late diagnostic items augment detail display but do
not change that immutable readiness decision, source identity, or archive
availability.

Package history is problem-level. Every user with problem read access sees the
same export jobs and Native materializations, including materializations
created as a side effect of contest builds. An available Native materialization
can be downloaded directly from the Packages page. Creating an export remains
a write-authorized operation.

A problem export artifact is identified by its published Native
materialization and export type. Export requests remain separate attempts with
separate job IDs; successful attempts may resolve to the same available
artifact. A failed attempt does not create or replace a cache hit.

Contest labels are placement metadata rather than problem export identity. A
contest package build consumes the canonical ICPC ZIP, safely extracts it into
the contest job staging tree, changes the single `short-name` entry in
`domjudge-problem.ini` to the frozen contest label, and repacks the result as a
contest-owned artifact. The changed ZIP is not inserted into the problem export
cache. Thus two contests can publish different labels from the same canonical
problem artifact without changing that artifact.

## ICPC export

ICPC export produces one hybrid ZIP; there are no selectable compatibility
profiles. The archive contains:

- `problem.yaml`
- `domjudge-problem.ini`
- language PDFs below `statement/`
- the legacy `problem_statement/` PDF mirror
- `data/sample/` and `data/secret/` testcase pairs
- `input_validators/` and, when configured, `output_validator/`
- categorized solutions and `submissions/submissions.yaml`
- copied `attachments/` when present

Representable pass-fail, interactive, and multi-pass problems use
`problem_format_version: 2025-09`. Interactive multi-pass falls back to
`problem_format_version: legacy`, because that combination is not represented
by the 2025-09 mode field used by the exporter.

Every rendered statement-language PDF is exported and mirrored into the legacy
directory. English is preferred for `problem_statement/problem.pdf`; when it is
absent, the first exported language is used.

Pass-fail sample tests are written below `data/sample/`. Interactive and
multi-pass samples are kept in `data/secret/` and omitted from rendered sample
blocks because the legacy DOMjudge sample path cannot express their execution
semantics.

The output aims at PPF 2025-09 and best-effort legacy DOMjudge consumption.
There is no compatibility release gate and no guarantee for every legacy
DOMjudge version. A canonical problem-level export uses the public problem slug
as the legacy DOMjudge `short-name`. The export cache may reuse an available
archive for the same materialization/type identity. If rebuilding is required,
ZIP bytes, timestamps, ordering effects, and SHA are not guaranteed to match an
earlier build.

## Polygon import

Polygon import accepts the supported Polygon archive layout, converts tests,
programs, configuration, and statements into the canonical workspace, and
preserves usable statement assets. Only members mapped by the supported layout
are converted; the external package is not stored as a parallel source model.

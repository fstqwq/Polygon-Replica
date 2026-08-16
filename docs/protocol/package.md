# Package import, verified revisions, and projections

The package lifecycle is one-way:

```text
published source -> verified revision -> package projections
```

A verified revision is not another source revision. It is the cleanup-safe
result of fully verifying one immutable published Git commit and retaining the
committed source, generated testcase inputs, and official answers needed by
delivery consumers.

## Import boundary

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
files. Their declared uncompressed sizes are checked before opening, and actual
decompressor output is charged again while streaming to an isolated staging
tree. `PROBLEM_ZIP_MAX_EXPANDED_BYTES` controls this consumed-byte budget; its
default is 256 MiB and its configured range is 64 MiB through 4 GiB. Unknown or
unconsumed members still count as entries but do not spend expanded-byte budget.
ZIP64 is accepted within these limits; no compression-ratio rule exists.

Each parsed XML, YAML, INI, or JSON document and the retained metadata set are
bounded by the fixed 4 MiB metadata limit. Import writes canonical workspace
source and discards the uploaded archive after conversion.

The application detects Polygon packages by `problem.xml`, ICPC/DOMjudge
packages by `problem.yaml`, and Polygon Replica packages by
`config/problem.json`. Their importers convert accepted files
into the current workspace source model and report validation errors at the
archive boundary.

Polygon resource discovery recognizes an optional `examples.tex` by basename
alongside `statements.ftl`, `problem.tex`, and `olymp.sty`. It is copied to
`statement/examples.tex` when present at `files/examples.tex` or at a declared
resource path. Packages without that resource retain the canonical renderer
fallback and do not gain a new authored file.

Importing as a new problem converts into an unborn workspace, commits the
result, and pushes `main`. Importing into an existing problem converts in a
staging directory, overwrites paths present in the converted tree, and keeps
existing paths absent from that tree; it leaves the merged changes uncommitted.

A Polygon Replica package contains a complete verified-revision payload, but
source import does not transfer verified-revision identity. Native source is
identified by `config/problem.json`. The importer selects only canonical
authored roots; `test-data/`, `statement-build/`, and every other unknown
top-level member all remain unopened under the same rule. It validates and
imports only authored source. Generated inputs, answers, verification
provenance, and offline statement products never enter the destination
workspace, Git, or materialization tables. The imported problem must run its
own Verification before it has a verified revision.

The ICPC importer accepts standard 2025-09 packages and the supported
DOMjudge-compatible layout. It parses scalar or sequence problem types,
statement layouts, validators, verdict directories, `submissions.yaml`, and
DOMjudge expected-result annotations into one canonical workspace shape.

Agent workspace compare/apply uses the same file-backed archive admission and
consumed-byte accounting. Its selected files are streamed into a temporary
workspace tree before comparison or replacement.

## Verified revision identity and contents

At most one verified-revision record exists for a `(problem_id, source_commit)`
pair. It keeps the published revision number, source digest, verification
provenance, archive locator, archive size and SHA-256, timestamps, and current
availability. Rebuilding the same Git revision reuses that identity.

The Polygon Replica package keeps the committed source at its root without
renaming `statement/`. It adds two package-owned derived trees:

```text
statement/
  statements.ftl
  problem.tex
  examples.tex       # present only when authored
  olymp.sty

test-data/
  manifest.json
  tests/
    <test-id>/
      input
      answer
      sample-input
      sample-output

statement-build/
  <language>/
    statements.tex
    problem.tex
    examples.tex
    olymp.sty
    ... copied statement assets and sample text files
```

Only applicable members are present. Interactive tests may omit `answer`.
Sample display files occur only when they differ from judge input or answer.
Every newly materialized native package renders every authored statement
language into `statement-build/`. The language directory is a complete TeX
working directory; offline compilation starts there with `statements.tex`, for
example:

```text
cd statement-build/english
xelatex -interaction=nonstopmode -halt-on-error statements.tex
```

This render consumes authored statement source plus an ephemeral
`StatementExamplesBundle` projected from the full Verification that created the
verified revision. Browser Preview uses the same producer with its sample-only
Verification, so pass order and resource bytes follow one contract. The bundle
does not backfill source from `test-data/`, modify `tests/spec.json`, or become a
manifest field.

`test-data/` is not Git source, but it is the verified payload consumed when the
platform opens a verified revision or builds a package projection. Those read
paths validate its manifest, checksums, paths, and complete inventory. Source
import deliberately does none of that because it neither consumes nor stores
the verified payload. `statement-build/` is a reproducible convenience product:
it is excluded from the source digest, is not interpreted during source import,
and does not add fields to the manifest. The exact package directory name is
`test-data`; the former underscore spelling is not part of this protocol.

The manifest contains `source_commit`, `revision_number`, `source_digest`,
`mode`, `pass_limit`, verification provenance, and the ordered tests. Each
test records its identity, source kind, sample flag, and descriptors for its
available payloads. A descriptor contains a canonical path, SHA-256, and size.
This is the current complete shape; it does not carry a speculative
project-owned format or materializer version.

Opening a verified revision checks the stored archive size and SHA, safe ZIP
member paths and types, manifest identity, every declared payload, absence of
undeclared testcase payloads, canonical source shape, and committed-source
digest. Both `test-data/` and `statement-build/` are excluded from that source
digest. A frozen consumer can additionally require the checksum recorded at
admission. The reader exposes only the extracted, validated revision; it does
not expose Git, a workspace, verification rows, or runtime cache references.

Availability, Git provenance, and current publication are separate facts. An
older verified revision remains usable after `main` advances. A missing or
corrupt archive marks that verified revision unavailable and invalidates its
package projections; it does not remove the Git revision.

## Package Export

Package Export is the only workflow allowed to prepare a verified revision.
Admission freezes the current published `main` commit and permits only one
Package Export for the same problem/commit at a time. A competing request fails
immediately instead of waiting.

Derived-package jobs proceed through these observable phases:

```text
queued -> verifying -> packaging -> complete
```

A Native job omits `packaging` because the verified revision is the package it
prepares.

For the frozen commit, the worker:

1. reuses the verified revision when its complete integrity check succeeds;
2. otherwise removes an unavailable or corrupt payload and its projections,
   runs one full verification, and rebuilds the same verified-revision identity;
3. finishes immediately for a Native request, or builds or reuses the requested
   DOMjudge or ICPC 2025-09 projection.

The problem Packages page accepts `native`, `domjudge`, and `icpc-2025-09`.
`native` prepares the verified revision when necessary and creates no row in
`exports`; the Agent Package Export API continues to expose only the two derived
formats. Separate request attempts keep separate job IDs even when they resolve
to the same cached projection. Problem-level projection cache identity is the
verified revision and target format. A standalone DOMjudge package always
derives its short name from the public slug segment; Contest label projections
are temporary Contest-owned children and are not stored in this cache.

The Polygon Replica package is downloaded directly from verified-revision
history. That read creates neither an export job nor a projection row. Preparing
a missing current verified revision through the Native action creates a job but
not a projection. Package Export always targets the published revision frozen at
request time; it does not accept a historical revision selector.

## Projection boundary

A package projector accepts only:

- an already validated verified-revision reader;
- the target external format;
- canonical naming options; and
- a caller-owned empty staging directory.

It may render and compile statement source from that reader. It MUST NOT read
Git, a workspace, verification tables, runtime cache, or another package
projection. It MUST NOT run verification or write export, Contest, or job rows.
The caller owns atomic archive publication and persistence.

### ICPC Problem Package 2025-09

The `icpc-2025-09` projection emits the strict supported subset of that
external format:

- `problem.yaml` with `problem_format_version: 2025-09`;
- language PDFs below `statement/`;
- testcase pairs below `data/sample/` and `data/secret/`;
- `input_validators/` and optional `output_validator/` wrappers;
- `submissions/` and `submissions/submissions.yaml`; and
- authored attachments.

The `type` field is the scalar `pass-fail` for ordinary problems and a YAML
sequence containing `pass-fail` plus `interactive` and/or `multi-pass` for the
other execution modes. Combined mode does not fall back to legacy metadata. An
interactive testcase without a canonical answer receives the structurally
required empty `.ans`. Interactive
and multi-pass samples without standard interaction data remain secret rather
than inventing an interaction transcript.

Expected behavior is expressed by `submissions.yaml`. Submitted source bytes are
not annotated. The archive excludes `domjudge-problem.ini` and
`problem_statement/`.

### DOMjudge package

The `domjudge` projection emits a DOMjudge-compatible package rather than
claiming strict ICPC 2025-09 conformance. It contains:

- `domjudge-problem.ini` with the requested short name;
- DOMjudge-compatible scalar problem type and validation metadata in
  `problem.yaml`;
- `problem_statement/problem.pdf`;
- testcase data, validators, verdict directories, and attachments.

C and C++ validators and interactors in both projections include executable
`build` and `run` files. The build file compiles the copied source with
`DOMJUDGE` defined and produces the executable used by `run`. This keeps the
program contract explicit in both layouts instead of relying on an importer's
language inference.

It excludes `statement/` and `submissions/submissions.yaml`. Standard accepted,
wrong-answer, time-limit, and runtime-error submissions use their conventional
directories. Only the three mixed behaviors use a language-appropriate
`@EXPECTED_RESULTS@` annotation in the copied source. Standalone export uses the
public slug segment as the short name; Contest projection passes the frozen
roster label.

## Contest builds

Contest builds consume verified revisions that already exist. In one SQLite
writer transaction, admission checks for an active build, reads the ordered
roster, selects each problem's highest available verified revision, and inserts
the job and all frozen build items with their archive checksums. If any problem
has none, admission returns `not_ready` without creating a job or copying
Contest source.

Selection is not restricted to current `main`: readiness reports `current`,
`stale`, or `none`. A worker opens exactly the frozen identities and checksums.
It never starts Verification, calls Package Export, repairs an unavailable
revision, or falls back to an older revision within the job.

One build can request `statement_pdf`, `domjudge_bundle`, and
`icpc_2025_09_bundle`. Readers are shared across requested outputs. Statement
PDF reads statement source, verified samples, and assets directly. Package
bundles call the same projectors described above and place temporary child ZIPs
inside a Contest-owned outer bundle. Child packages do not become problem-level
exports.

Each package bundle is all-or-nothing: failure of one problem publishes no
bundle of that format. Different requested outputs remain independent, so the
job is `partial` when at least one output succeeds and another fails.
Package-only builds do not snapshot the Contest source filesystem.

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
time; it does not materialize all child payloads in memory. The configured
problem count is checked before a Contest row is created. Manual additions use
the same limit, with count, next position, and insert serialized in one SQLite
writer transaction. Lowering the limit does not disable an existing over-limit
Contest; it only rejects new roster entries.

## Cleanup

Verified revisions, their build rows and archives, projection-cache rows and
archives, Package Export jobs, and Contest build products are Derived data.
Generated Artifacts cleanup removes them. Published Git commits, workspaces,
Contest definitions and source, and operator backups remain. A cleaned Git
revision is simply no longer verified and can become a verified revision again
through a later Package Export.

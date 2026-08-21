# Package import, Native Packages, and external packages

The package lifecycle is one-way:

```text
published source -> Native Package -> external packages
```

A Native Package is not another source revision. It is the cleanup-safe result
of running the main correct solution for one immutable published Git commit and
retaining the committed source, generated testcase inputs, and official answers
needed by delivery consumers. A full Verification may produce the Package
directly or certify an existing Package whose evidence is identical. The
implementation stores its metadata as a package materialization and exposes its
validated archive through `NativePackageReader`.

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
packages by `problem.yaml`, and Native Packages by
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

A Native Package contains a complete materialized payload, but source import
does not transfer its materialization identity or certification. Native source
is identified by `config/problem.json`. The importer selects only canonical
authored roots; `test-data/`, `statement-build/`, and every other unknown
top-level member all remain unopened under the same rule. It validates and
imports only authored source. Generated inputs, answers, and offline statement
products never enter the destination workspace, Git, or materialization
tables. The imported problem must create its own Native Package.

The ICPC importer accepts standard 2025-09 packages and the supported
DOMjudge-compatible layout. It parses scalar or sequence problem types,
statement layouts, validators, verdict directories, `submissions.yaml`, and
DOMjudge expected-result annotations into one canonical workspace shape.

Agent workspace compare/apply uses the same file-backed archive admission and
consumed-byte accounting. Its selected files are streamed into a temporary
workspace tree before comparison or replacement.

## Native Package identity and contents

At most one Native Package materialization exists for a
`(problem_id, source_commit)` pair. Its internal native-package record keeps the
published revision number, source digest, current certification Verification
reference, archive locator, archive size and SHA-256, timestamps, and current
availability. Rebuilding the same Git revision reuses that identity. The
Package is certified only while this reference identifies a successful full
Verification for the same published commit; a package-only run leaves it
available but marked `not verified`.

The Native Package keeps the committed source at its root without
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
`StatementExamplesBundle` derived from the main-correct evidence that created
the Native Package. Browser Preview uses the same producer with its sample-only
Verification, so pass order and resource bytes follow one contract. The bundle
does not backfill source from `test-data/`, modify `tests/spec.json`, or become a
manifest field.

`test-data/` is not Git source, but it is the materialized payload consumed when
the platform opens a Native Package or runs an external-package adapter.
Construction validates its source, manifest, declared payloads, paths,
checksums, and complete inventory.
Source import deliberately does none of that because it neither consumes nor
stores the materialized payload. `statement-build/` is a reproducible
convenience product:
it is excluded from the source digest, is not interpreted during source import,
and does not add fields to the manifest. The exact package directory name is
`test-data`; the former underscore spelling is not part of this protocol.

The manifest contains `source_commit`, `revision_number`, `source_digest`,
`mode`, `pass_limit`, stable authored solution metadata, and the ordered tests.
Each test records its identity, source kind, sample flag, and descriptors for
its available payloads. A descriptor contains a canonical path, SHA-256, and
size. Verification IDs, observed verdicts, and solution verdict summaries are
not archive content. Readers validate the required current shape and ignore
unknown fields, so older Packages that contain those extra fields remain
readable. The manifest does not carry a speculative project-owned format or
materializer version.

When Verification skips a testcase because its generated-input artifact is
identical to another testcase's artifact, materialization resolves the unique
actually executed testcase with that artifact. The skipped testcase remains a
separate ordered package testcase: its input and any available answer are
copied from that owner. Statement-only sample overrides remain those of the
skipped testcase.

Native Package construction validates the canonical source tree, manifest, and
declared payloads before serialization. Native and external Package writers
validate their complete staging tree, then pass its root to a 7-Zip process in
ZIP mode using fastest deflate. ZIP entry ordering, timestamps, compression
streams, and complete archive bytes are not Package identity. A later reader
checks the materialization record's whole-archive SHA-256, safely extracts ZIP
members, and parses the manifest shape required by the consumer. A validated
`NativePackageReader` therefore means archive-level integrity and safe
extraction have completed; it does not repeat construction-time inventory
validation. It does not rehash individual payloads,
rebuild the source digest, or rescan the archive when the reader closes. A
frozen consumer can
additionally require the checksum recorded at admission. The reader exposes
only the extracted Package; it does not expose Git, a workspace, Verification
rows, or runtime cache references.

Availability, Git provenance, and current publication are separate facts. An
older Native Package remains usable after `main` advances. A missing or corrupt
archive marks that Native Package unavailable and invalidates its cached
external packages; it does not remove the Git revision.

## Package Export

Package Export is the only workflow allowed to prepare a Native Package.
Admission freezes the current published `main` commit and permits only one
Package Export for the same problem/commit at a time. A competing request fails
immediately instead of waiting.

Derived-package jobs proceed through these observable phases:

```text
queued -> verifying -> packaging -> complete
```

A Native job omits `packaging` because its result is the Native Package itself.

For the frozen commit, the worker validates any existing Native Package before
reuse. A normal Native request reuses a certified Package; an available but
uncertified Package runs the missing full Verification and updates only its
certification reference when every input and answer matches. The optional
`Run standard solution only` mode reuses any valid Package or, when none exists,
runs only input generation and the main correct solution to create one marked
`not verified`. The same option applies when the request also asks for an
external package: the worker prepares the missing Native Package with only the
main correct solution, then runs the selected adapter. A later successful full
Verification certifies it without rewriting its archive or cached external
packages. Evidence mismatch leaves the Package unchanged and reports a
diagnostic.

An unavailable or corrupt payload and its cached external packages are removed
before rebuilding the same materialization identity. External-package requests
may use a valid uncertified Native Package. When no Package exists, they prepare
one through full Verification by default or through standard-solution-only
Verification when explicitly requested, then run or reuse the requested
adapter.

The problem Packages page accepts `native`, `domjudge`, `icpc-2025-09`, `qoj`,
and `nowcoder`.
`native` prepares the Native Package when necessary and creates no row in
`exports`; the Agent Package Export API exposes the four external formats.
Separate request attempts keep separate job IDs even when they resolve
to the same cached external package. Problem-level external-package cache
identity is the Native Package materialization and target format. A standalone
DOMjudge package always derives its short name from the public slug segment;
Contest-indexed packages are temporary Contest-owned children and are not
stored in this cache.

The Native Package is downloaded directly from package history. That read
creates neither an export job nor an `exports` row. Preparing a missing current
Native Package through the Native action creates a job but no external package.
Package Export always targets the published revision frozen at request time; it
does not accept a historical revision selector.

## Package adapter boundary

A package adapter accepts only:

- an already validated `NativePackageReader` for a Native Package;
- the target external format;
- canonical naming options; and
- a caller-owned empty staging directory.

It may render and compile statement source from that reader. It MUST NOT read
Git, a workspace, verification tables, runtime cache, or another package
adapter's output. It MUST NOT run Verification or write export, Contest, or job
rows.
The caller owns atomic archive publication and persistence.

A package adapter MUST NOT execute problem source or invoke a source compiler
inside the application process or its local bubblewrap sandbox. Format-specific
source compatibility checks MUST use the Judgehost compile-only workflow. A
failed compatibility check may become a package warning when the target format
defines it as advisory; it does not authorize a local compiler fallback.

### ICPC Problem Package 2025-09

The `icpc-2025-09` adapter emits the strict supported subset of that
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

The `domjudge` adapter emits a DOMjudge-compatible package rather than
claiming strict ICPC 2025-09 conformance. It contains:

- `domjudge-problem.ini` with the requested short name;
- DOMjudge-compatible scalar problem type and validation metadata in
  `problem.yaml`;
- `problem_statement/problem.pdf`;
- testcase data, validators, verdict directories, and attachments.

C++ validators and interactors in both external packages include executable
`build` and `run` files. The build file compiles the copied source with
`DOMJUDGE` defined and produces the executable used by `run`. C component
sources are unsupported and are not renamed or translated to C++. This keeps
the program contract explicit in both layouts instead of relying on an
importer's language inference.

It excludes `statement/` and `submissions/submissions.yaml`. Standard accepted,
wrong-answer, time-limit, and runtime-error submissions use their conventional
directories. Only the three mixed behaviors use a language-appropriate
`@EXPECTED_RESULTS@` annotation in the copied source. Standalone export uses the
public slug segment as the short name; a Contest download passes the current problem
index to the adapter. Standalone color is selected deterministically from the
Problem external ID. Contest downloads instead assign the adapter's fixed
18-color palette in canonical roster order and repeat it from the first color
after the eighteenth problem.

The Contest palette is, in order: `#e6194b` (red), `#4363d8` (blue),
`#ffe119` (yellow), `#3cb44b` (green), `#f58231` (orange), `#6b2c91`
(purple), `#eeeeee` (white), `#9a6324` (brown), `#46d9e6` (cyan),
`#303030` (black), `#ff6f91` (pink), `#9bdc28` (lime), `#9e9e9e`
(silver), `#008080` (teal), `#d4a017` (gold), `#800000` (burgundy),
`#aaffc3` (mint), and `#ffd8b1` (peach).

### QOJ package

The `qoj` adapter emits the source data archive consumed by QOJ's **Sync Test
Data** operation. Archive members are at the ZIP root. The adapter writes
ordered verified testcase pairs as `1.in`, `1.ans`, and so on, and duplicates
the verified display payload of each sample as `ex_1.in`, `ex_1.ans`, and so on.
An interactive testcase without a canonical answer receives an empty `.ans`;
the adapter does not reinterpret a transcript or jury log as an answer.

`problem.conf` uses the built-in judger, one 100-point subtask, and the
published time and memory limits. QOJ's documented 6144 MiB memory ceiling is
enforced. The adapter supports one or two passes. Two-pass output uses
`polygon_runtwice`; combined
interactive/two-pass output also uses `polygon_runtwice_interactive` and
`interactor_run_type default`. A higher pass limit is rejected rather than
inventing a custom manager or judger.

A checker that is byte-identical to the vendored `ncmp.cpp`, `wcmp.cpp`, or
`fcmp.cpp` becomes the corresponding `use_builtin_checker` value. Other checker
source is copied as `chk.cpp`. A pass-fail problem without a checker receives a
deterministic byte-exact `chk.cpp`; it is not mapped to whitespace-insensitive
`wcmp`. Interactive packages use `irscmp` and copy the configured interactor as
`interactor.cpp`.

The accepted solution, validator, and interactor are copied as QOJ Sync source
inputs named `std.<extension>`, `val.cpp`, and `interactor.cpp`. The adapter does
not compile those programs or publish QOJ's generated executables. Missing
`std` or `val` produces a warning that Hack must be disabled. QOJ Sync owns
program compilation, testcase validation and normalization, and the resulting
diagnostics.

The adapter compiles the preferred verified statement build to root
`statement.pdf`, choosing English when present and otherwise the first authored
language. Authored participant attachments are written below `download/` with
their relative paths preserved. The adapter does not create `download.zip`;
QOJ Sync packages `download/` for contestants.

This supported subset follows the
[QOJ problem-data guide](https://qoj.ac/blog/qingyu/blog/1423). It does not emit
`require/`, a custom judger or manager, communication mode, submit-answer mode,
or custom scoring and subtask dependencies.

### Nowcoder package

The `nowcoder` adapter accepts only a pass-fail problem with `pass_limit=1`.
It writes the ordered verified testcase pairs at the archive root as `1.in`,
`1.ans`, `2.in`, `2.ans`, and so on. When the problem selects a custom checker,
the adapter copies that source to `checker.cc`.

Nowcoder uses an older `testlib.h` and a C++14 compiler. The adapter does not
claim to verify general compatibility with that toolchain. It performs only the
most basic check for the literal `setTestCase`: this project's checker guideline
recommends that API, while Nowcoder's older `testlib.h` may not provide it. A
match is reported as an advisory Package Export warning; the archive is still
published. The adapter does not compile the checker.

## Contest package downloads

The Contest Problems page offers `Build All Packages` until every roster
problem's current published revision has a ready Native Package. Once all are
ready, that action becomes `Download Packages`. The download dialog enumerates
the registered external adapters in their stable order: DOMjudge, ICPC Problem
Package 2025-09, QOJ, and Nowcoder.

The POST request blocks until the selected bundle has been built. It reads the
current roster in canonical `idx` order, rechecks published-package readiness,
and opens each exact Native Package with its recorded archive checksum. A stale
or missing Package rejects the request; the service never falls back to an
older Package, starts Verification, or repairs an unavailable Package.

The bundle invokes the same adapters described above and supplies each adapter
with the Contest `idx` and canonical ordinal. DOMjudge uses that placement for
its short name and balloon color; strict ICPC, QOJ, and Nowcoder output does not
embed it. Child ZIP files exist only inside one temporary outer bundle and do
not become problem-level external-package cache entries. Failure of any child
package aborts the entire download.

The outer ZIP contains only `packages/<idx>-<problem>.zip` children. It does not
add a manifest or expose Git commits, Native Package identities, or archive
checksums. Because every child is already a compressed ZIP, the outer archive
stores those files without compressing them again.

The response creates no Contest job, frozen build-item rows, history entry, or
durable Contest artifact. Its request-owned temporary directory is deleted
after the file transfer. Contest HTML and PDF statements use the separate
Statement Preview lifecycle and are not package-download outputs.

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
the same limit, with count and insertion serialized in one SQLite writer
transaction. The supplied `idx` is unique within the Contest; no position is
allocated or stored. Lowering the limit does not disable an existing over-limit
Contest; it only rejects new roster entries.

## Cleanup

Native Package materializations and archives, external-package cache rows and
archives and Package Export jobs are Derived data. Contest package bundles are
transient response files.
Generated Artifacts cleanup removes them. Published Git commits, workspaces,
Contest definitions and source, and operator backups remain. A cleaned Git
revision simply has no Native Package and can be packaged again through a later
Package Export.

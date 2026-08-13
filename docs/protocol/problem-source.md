# Problem source protocol

## Authority and revisions

Committed problem source is owned by Git. A published problem is the commit at
the problem repository's `main` reference. A workspace is a mutable per-user
checkout; SQLite stores its identity, owner, base revision, and status but not
the committed file contents.

Authored entries are limited to `attachments/`, `checkers/`, `config/`,
`generators/`, `interactors/`, `solutions/`, `statement/`,
`statement-assets/`, `statement-sections/`, `tests/`, `third_party/`, and
`validators/`. They are regular files and directories; symbolic links,
special files, hidden paths, and materialized answer paths are invalid. The
workspace's `.git` metadata is outside the authored tree. Native
`test_data/` is validated by the package manifest and is never authored source.

## Problem configuration

`config/problem.json` is a required UTF-8 JSON object with exactly these
fields:

- `time_limit_ms`: 100 through 30000; default 2000
- `memory_limit_mb`: 1 through 2048; default 1024
- `mode`: `pass-fail` or `interactive`; default `pass-fail`
- `pass_limit`: 1 through 64; default 1

The service creates these defaults only when it creates a new problem. A reader
never supplies a missing field, clamps an authored value, or replaces a missing
or malformed file. UI saves, imported source, verification, preview,
publication readiness, and package materialization all use this codec.
Execution dispatches the accepted values without another memory floor.

`config/build.json` is also a required UTF-8 JSON object. Its only fields are
the optional selections `accepted_solution_source`,
`validator_source`, `checker_source`, and `interactor_source`. All paths are
normalized relative POSIX paths. Solutions live directly under `solutions/`;
validator, checker, and interactor selections point to C++ source below their
matching roots.

For these four fields, an absent selection means that component is not selected.
Runtime code does not fill a missing selection by scanning a directory or
choosing a conventional filename. Interactive source rejects a checker
selection, and pass-fail source rejects an interactor selection. External import
adapters may infer a best-effort selection, but they write the result into this
object before any authored-source consumer runs.

## Test specification

`tests/spec.json` is a required UTF-8 JSON object whose only field is `tests`.
The array order is testcase order. Each entry contains:

- `id`: a unique string of 3 through 12 decimal digits
- `kind`: `manual` or `gen`
- optional `sample`: a boolean whose absence means `false`
- optional `sample_input` and `sample_output` strings
- optional `sample_output_validate`, a boolean whose absence means `true`

Unknown or duplicate object keys, a top-level array, string booleans, normalized
aliases, duplicate IDs, and other scalar types are invalid.

The serialized file is bounded by `TEXTAREA_MAX_BYTES` (256 KiB by default).
For each sample entry, the combined UTF-8 bytes of `sample_input` and
`sample_output` are independently bounded by `STATEMENT_SAMPLE_MAX_BYTES`
(32 KiB by default). The same per-sample limit applies when import, preview, or
package rendering fills missing display data from materialized test input and
answer files. Oversized samples are rejected rather than truncated. Different
sample entries do not share another aggregate budget; the serialized-file
limit remains their common envelope.

Judge input is not embedded in the entry. A manual test reads
`tests/manual/<id>.in`. A generated test reads
`tests/generator/<id>.in` as a shell-word command: its first token resolves a
source below `generators/`, and the remaining tokens are its arguments. A
missing payload, missing generator, or ambiguous generator token invalidates
the source tree. An empty `tests` array has no implicit discovery behavior;
verification and Native materialization require at least one explicit test.

The runtime generator input payload is the generator executable invocation plus
these command parameters. Its execution identity and scheduling semantics are
defined by the [execution protocol](execution.md).

Configured source programs live under the established roots such as
`generators/`, `validators/`, `checkers/`, and `solutions/`. The generator's
configured output-checking component is selected from `validators/`.

## Solutions

Solution programs live directly below `solutions/`; the UI currently lists
`.cpp`, `.cc`, `.cxx`, `.c++`, `.py`, and `.java` files. Optional metadata for a
source is stored next to it as `<source>.desc`. When a descriptor is needed, the
canonical writer emits `expected: <behavior>` and zero or more `note: <text>`
lines. It omits a descriptor for `unknown` with no note. Current behavior values
are `accepted`, `wrong_answer`, `tle_or_correct`, `tle_or_re`,
`time_limit_exceeded`, `run_time_error`, `rejected`, and `unknown`.

A missing descriptor means `unknown`. When a descriptor exists, `expected`
occurs exactly once; each non-empty line is either `expected: ...` or
`note: ...`. `behavior:`, `verdict:`, and unkeyed lines are invalid. The
configured `accepted_solution_source` is the sole main-correct selection and
has effective behavior `accepted`, regardless of whether it has a descriptor.
Selecting it does not create or rewrite a descriptor. No filename or list-order
inference is performed at runtime. Polygon and ICPC importers may infer external
intent and materialize descriptors for other behaviors before returning the
imported workspace.

## Native import and preflight

A Native package contains the committed source tree at its root plus a complete
`test_data/manifest.json` and its declared materialized test payloads. Import
validates the manifest shape, source digest, test order, payload paths, sizes,
checksums, file types, and the complete `test_data/` inventory. It then discards
the entire `test_data/` tree and imports only authored source. Materialized
answers never enter the destination workspace or Git history.

Operators can inspect all published `main` revisions without mutating Git:

```text
PYTHONPATH=. python scripts/check_problem_sources.py --db <sqlite> --bare-root <repos>
```

The command opens SQLite read-only, extracts each published commit to a
temporary directory, and reports path-specific canonical-source errors. It does
not rewrite a repository or create a workspace.

## Statements

Statement source languages are directories under `statement-sections/`. A
language directory MUST exist to select that language; individual section files
may be absent or empty. The recognized section files are:

- `name.tex`
- `legend.tex`
- `input.tex`
- `output.tex`
- `interaction.tex`
- `notes.tex`

Missing or empty `name.tex` falls back to the problem slug/title chosen by the
current statement context. Other missing sections render as empty content.
`scoring.tex` is ignored by the current renderer rather than treated as a
seventh section.

Shared statement assets live under `statement-assets/`. The files
`statement/statements.ftl`, `statement/problem.tex`, and `statement/olymp.sty`
are editable rendering source. `statement/main.tex`, everything below
`statement/rendered/`, and PDFs are regenerated products.

Statement languages are ordered as English, Chinese, then alphabetically. The
renderer obtains samples from `tests/spec.json`: explicit `sample_input` and
`sample_output` override judge data for display, while missing sample data may
be filled in a preview snapshot by a sample-only verification.

## Publication

Publishing refuses a workspace based on an older published revision, commits
the workspace, and pushes `main`. If the push fails, it attempts to roll back
the new local commit. Verification signatures use the relevant source paths and
canonical configuration. Verification cache payloads and derived packages do
not replace Git provenance: their rows retain the source commit they were built
from.

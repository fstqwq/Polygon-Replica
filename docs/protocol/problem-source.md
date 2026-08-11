# Problem source protocol

## Authority and revisions

Committed problem source is owned by Git. A published problem is the commit at
the problem repository's `main` reference. A workspace is a mutable per-user
checkout; SQLite stores its identity, owner, base revision, and status but not
the committed file contents.

Paths supplied through HTTP or package archives MUST be relative, normalized,
remain inside the workspace, and not traverse symlinks outside it.

## Problem configuration

`config/problem.json` is the authored runtime configuration. The settings UI
writes these canonical fields:

- `time_limit_ms`: 100 through 30000; default 2000
- `memory_limit_mb`: 1 through 2048; default 1024
- `mode`: `pass-fail` or `interactive`; default `pass-fail`
- `pass_limit`: 1 through 64; default 1

The settings reader uses all four defaults when the file is absent. When the
file exists, it requires a JSON object containing `mode` and `pass_limit`; the
two limit fields remain optional and are clamped to their authoring ranges.
Verification also defaults a missing or unreadable file, but Native
materialization and ICPC export require the published `config/problem.json`.
UI saves, manually edited source, and imported source all enter execution
through this same normalization; execution dispatches the normalized value
without an additional memory floor.

`config/build.json` records optional source selections. Its canonical ordered
selection keys are `accepted_solution_source`, `validator_source`,
`checker_source`, `interactor_source`, and the ordered `generator_sources`
array. Each selected source is a normalized workspace-relative path below its
corresponding source directory. When a component selection is absent,
verification and export choose the lexicographically first eligible source in
that component directory. A directory with multiple eligible sources and no
selection is non-canonical import input: this fallback is best effort and does
not guarantee that the author's intended checker, validator, or interactor was
chosen. A configured path is the only unambiguous selection.

Execution also reads `checker_args`. When `tests/spec.json` is absent or empty,
it discovers `.in` files below `tests/manual/` and runs every configured
`generator_sources` entry `generator_runs` times with the shared
`generator_args`; the defaults are three runs and no arguments. These fallback
keys are preserved by the build-config writer but are not source-selection
keys.

## Test specification

`tests/spec.json` is the ordered testcase definition. The canonical writer emits
a JSON object with a `tests` array; the reader also accepts that array directly.
Each entry has:

- `id`: a unique string of 3 through 12 decimal digits
- `kind`: `manual` or `gen`
- `sample`: a boolean
- optional `sample_input` and `sample_output` strings
- optional `sample_output_validate`, which defaults to `true`

The serialized file is bounded by `TEXTAREA_MAX_BYTES` (256 KiB by default).
For each sample entry, the combined UTF-8 bytes of `sample_input` and
`sample_output` are independently bounded by `STATEMENT_SAMPLE_MAX_BYTES`
(32 KiB by default). The same per-sample limit applies when import, preview, or
package rendering fills missing display data from materialized test input and
answer files. Oversized samples are rejected rather than truncated. Different
sample entries do not share another aggregate budget; the serialized-file
limit remains their common envelope.

The payload is not embedded in the entry. A manual test reads
`tests/manual/<id>.in`. A generated test reads
`tests/generator/<id>.in` as a shell-word command: its first token resolves a
source below `generators/`, and the remaining tokens are its arguments.

The runtime generator input payload is the generator executable invocation plus
these command parameters. Its execution identity and scheduling semantics are
defined by the [execution protocol](execution.md).

Configured source programs live under the established roots such as
`generators/`, `validators/`, `checkers/`, and `solutions/`. The generator's
configured output-checking component is selected from `validators/`.

## Solutions

Solution programs live directly below `solutions/`; the UI currently lists
`.cpp`, `.cc`, `.cxx`, `.c++`, `.py`, and `.java` files. Metadata for a source is
stored next to it as `<source>.desc`. The canonical writer emits
`expected: <behavior>` and zero or more `note: <text>` lines. Current behavior
values are `accepted`, `wrong_answer`, `tle_or_correct`, `tle_or_re`,
`time_limit_exceeded`, `run_time_error`, `rejected`, and `unknown`.

The reader also recognizes `behavior` and `verdict` as the expected-behavior key
and treats an unkeyed line as note text. When a descriptor is missing, the
effective behavior is inferred from filename tokens such as `ac`, `wa`, `tle`,
and `re`; an unrecognized name becomes `unknown`. A present descriptor replaces
that inferred value. The accepted solution is the configured
`accepted_solution_source`, otherwise the first solution whose effective
behavior is `accepted`, otherwise the only visible solution.

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
canonical configuration. Derived verification or package artifacts never
replace Git provenance: their rows retain the source commit they were built
from.

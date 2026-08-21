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
workspace's `.git` metadata is outside the authored tree. A Native Package's
`test-data/` and `statement-build/` trees are never authored source.

## Problem configuration

`config/problem.json` is a required UTF-8 JSON object with exactly these
fields:

- `time_limit_ms`: 100 through 30000; default 2000
- `memory_limit_mb`: 1 through 2048; default 1024
- `mode`: `pass-fail` or `interactive`; default `pass-fail`
- `pass_limit`: 1 through 64; default 1

The service writes these defaults when it creates a new problem. Strict source
consumers never supply a missing field, clamp an authored value, or replace a
missing or malformed file. Authoring pages are different: they show the
configuration error under Review and Publish and use the defaults only to keep
the editor operable. Saving the General form writes one complete canonical
object. Verification, Package Export, Contest package downloads, and Native Package
construction still reject the invalid source at their entrance.
Execution dispatches the accepted values without another memory floor.

`config/build.json` is also a required UTF-8 JSON object. Its fields are the
optional selections `accepted_solution_source`, `validator_source`,
`checker_source`, and `interactor_source`, plus the optional
`generator_sources` array. All paths are normalized relative POSIX paths.
Solutions live directly under `solutions/`; validator, checker, and interactor
selections point to C++ source below their matching roots. Missing
`generator_sources` means an empty generator allowlist. When present, its
entries are unique source paths below `generators/`.

For these four fields, an absent selection means that component is not selected.
Runtime code does not fill a missing selection by scanning a directory or
choosing a conventional filename. Interactive source rejects a checker
selection, and pass-fail source rejects an interactor selection. External import
adapters may infer a best-effort selection, but they write the result into this
object before any authored-source consumer runs.

Saving a checker, validator, interactor, or generator in the Problem editor validates write access, the normalized component path, the UTF-8 source-size limit, and the resulting source selection before persisting the edit. Saving does not compile the source or contact a Judgehost. Syntax and toolchain errors are reported by Verification, Package construction, or an explicit compatibility check that actually requests compilation.

An authoring read recognizes the removed build fields from the previous source
shape. For a writable workspace it deletes those obsolete fields and preserves
the four current selections exactly. This is a visible workspace modification
and Review and Publish reports it until the normalized source is published.
Unknown fields, malformed JSON, invalid paths, and missing selected files are
diagnosed but never guessed or silently rewritten. In particular, a missing
`accepted_solution_source` is not inferred from a filename, list order, or an
`expected: accepted` solution descriptor.

## Test specification

`tests/spec.json` is a required UTF-8 JSON object whose only field is `tests`.
The array order is testcase order. Each entry contains:

- `id`: a unique string of 3 through 12 decimal digits
- `kind`: `manual` or `gen`
- optional `sample`: a boolean whose absence means `false`
- optional `sample_input` and `sample_output` strings
- optional `sample_output_validate`, a boolean whose absence means `true`
- optional `sample_json`, a structured `pair` or `interaction` sample object

`sample_json` is mutually exclusive with `sample_input` and `sample_output`.
Pair samples contain one or more contiguous passes with inline `input` and
`output` strings. Interaction samples contain contiguous passes whose ordered
events each provide `source` (`interactor` or `solution`) and inline `content`.
The Tests page validates the same strict shape before submission; unknown
fields remain invalid on the server.

```json
{
  "sample": true,
  "sample_json": {
    "presentation": "pair",
    "passes": [
      {"number": 1, "input": "1\n", "output": "2\n"},
      {"number": 2, "input": "2\n", "output": "3\n"}
    ]
  }
}
```

For an interactive sample, `presentation` is `interaction` and each pass has
an `events` array such as
`{"source":"interactor","content":"1\n"}`. Pass numbers start at one and
are contiguous. Event array order is the displayed protocol order.

Unknown or duplicate object keys, a top-level array, string booleans, normalized
aliases, duplicate IDs, and other scalar types are invalid.

The serialized file is bounded by `TEXTAREA_MAX_BYTES` (256 KiB by default).
For each sample entry, the combined UTF-8 bytes of `sample_input` and
`sample_output`, or all inline `sample_json` content, are independently bounded
by `STATEMENT_SAMPLE_MAX_BYTES`
(32 KiB by default). The same per-sample limit applies when import, preview, or
package rendering fills missing display data from materialized test input and
answer files. Oversized samples are rejected rather than truncated. Different
sample entries do not share another aggregate budget; the serialized-file
limit remains their common envelope.

Judge input is not embedded in the entry. A manual test reads
`tests/manual/<id>.in`. A generated test reads
`tests/generator/<id>.in` as a shell-word command: its first token resolves a
source selected by `generator_sources`, and the remaining tokens are its
arguments. A missing payload, unselected or missing generator, or ambiguous
generator token invalidates the source tree. Files merely present below
`generators/` are not executable inputs until selected. An empty `tests` array
has no implicit discovery behavior; Verification and Native Package
construction require at least one explicit test.

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
`time_limit_exceeded`, `run_time_error`, `compile_error`, `rejected`, and `unknown`.
The system does not accept or dispatch `.c` solution sources.

A missing descriptor means `unknown`. When a descriptor exists, `expected`
occurs exactly once; each non-empty line is either `expected: ...` or
`note: ...`. `behavior:`, `verdict:`, and unkeyed lines are invalid. The
configured `accepted_solution_source` is the sole main-correct selection and
has effective behavior `accepted`, regardless of whether it has a descriptor.
Selecting it does not create or rewrite a descriptor. No filename or list-order
inference is performed at runtime. Polygon and ICPC importers may infer external
intent and materialize descriptors for other behaviors before returning the
imported workspace.

## Native Package import and preflight

A Native Package contains the committed source tree at its root plus a
complete `test-data/manifest.json`, its declared verified test payloads, and a
derived `statement-build/<language>/` offline TeX tree.
Import identifies native source by `config/problem.json` and selects only the
canonical authored roots. It treats both derived trees exactly like any other
unknown package members: they are not opened, parsed, checksummed, or persisted.
Generated answers never enter the destination workspace or Git history, and the
imported problem does not inherit the source problem's verification provenance
or Native Package materialization identity.

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
are editable rendering source. `statement/examples.tex` is optional editable
source shared by every statement language. It is absent by default; the
renderer uses the canonical examples template when it is missing. Creating the
file opts into a repository-owned override, while deleting it restores the
canonical fallback. An authored empty file is valid and deliberately renders
an empty examples companion.

`statement/main.tex`, everything below `statement/rendered/`, and PDFs are
regenerated products. Every language render also writes a derived
`statement/rendered/<language>/examples.tex`. The default `problem.tex`
unconditionally inputs that companion. Both the problem and examples FTL
templates receive the same render context. Existing custom problem templates
are not rewritten for compatibility; they must input `examples.tex` themselves
to use either the canonical or authored companion.

Native package construction leaves this authored layout unchanged and writes a
separate `statement-build/<language>/` directory. Each such directory contains
rendered `statements.tex`, `problem.tex`, and `examples.tex`, a copied
`olymp.sty`, referenced assets, and generated sample text files. Neither
`statement-build/` nor `test-data/` participates in the committed-source digest
or may be committed as problem source. Building this tree projects sample
presentation from the same canonical Verification pass evidence used by Test
Details. The resulting context and text files stay within the derived
render-resource boundary.

Statement languages are ordered as English, Chinese, then alphabetically. The
producer obtains sample order and explicit display overrides from
`tests/spec.json`. Browser Preview may run a sample-only Verification. Package
Export runs full Verification by default or a standard-solution-only run when
requested; both consume the same main-correct per-pass artifacts without
modifying the source snapshot.

The canonical examples template preserves Polygon compatibility by rendering
`problem.sampleTests[].inputFile` and `.outputFile` through `\exmpfile`. It also
accepts an optional structured extension at `problem.examples.samples`:

```text
samples[].number
samples[].presentation = pair | interaction
samples[].passes[].number

pair pass:
  inputFile
  outputFile

interaction pass:
  events[].source = interactor | solution
  events[].textFile
```

The rendered structured extension uses controlled resource paths and is not
written back into authored source. `StatementExamplesProducer` creates it from
either an authored inline `sample_json` or the canonical main-correct
`ExecutionResult.passes` projection. Event order and pass numbers are explicit;
there is no inferred alternation, event `kind`, or EOF entry.
Presence is authoritative, so an explicitly empty `samples` array does not fall
back to Polygon samples. The renderer writes the bundle's controlled UTF-8 text
resources relative to the rendered problem compile directory.

## Publication

Publishing refuses a workspace based on an older published revision, commits
the workspace, and pushes `main`. If the push fails, it attempts to roll back
the new local commit. Verification signatures use the relevant source paths and
canonical configuration. Verification cache payloads and derived packages do
not replace Git provenance: their rows retain the source commit they were built
from.

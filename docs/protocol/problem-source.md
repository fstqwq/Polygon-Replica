# Problem source protocol

## Authority and revisions

Committed problem source is owned by Git. A published problem is the commit at
the problem repository's `main` reference. A workspace is a mutable per-user
checkout; SQLite stores its identity, owner, base revision, and status but not
the committed file contents.

Paths supplied through HTTP or package archives MUST be relative, normalized,
remain inside the workspace, and not traverse symlinks outside it.

## Runtime configuration

`config/problem.json` stores the authored runtime limits. The memory limit is an
integer from 1 through 2048 MiB and defaults to 1024 MiB when it is absent or
cannot be parsed. Authoring clamps values below 1 to 1 and values above 2048 to
2048. UI saves, manually edited source, and imported source all enter execution
through this same normalization.

## Test specification

`tests/spec.json` is the ordered testcase definition. Entries are manual or
generator-backed and carry their stable id plus source metadata. Manual payloads
live under `tests/manual/`; generator definitions reference programs under
`generators/` and payloads under `tests/generator/` as accepted by the current
parser.

The complete generator input payload includes its command parameters. That
payload participates in testcase, execution, and cache identity; two different
parameter lists do not describe the same generated input.

Configured source programs live under the established roots such as
`generators/`, `validators/`, `checkers/`, and `solutions/`. The generator's
configured checker is the validator for generated output. Verification does not
insert a second standalone validator task into the DAG.

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
Shared statement assets live under `statement-assets/`. Rendered templates and
PDFs are derived products and are not the authority for editable statement
source.

## Publication

Publishing records a Git commit and updates the published reference after
workspace checks. Verification signatures use the relevant source paths and
canonical configuration. Derived verification or package artifacts never
replace Git provenance: their rows retain the source commit they were built
from.

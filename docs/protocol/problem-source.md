# Problem source protocol

## Authority and revisions

Committed problem source is owned by Git. A published problem is the commit at the problem repository's `main` reference. A workspace is a mutable per-user checkout; SQLite stores its identity, owner, base revision, and status, but not committed file contents.

| Source category | Location or rule |
| --- | --- |
| Authored roots | `attachments/`, `checkers/`, `config/`, `generators/`, `interactors/`, `solutions/`, `statement/`, `statement-assets/`, `statement-sections/`, `tests/`, `third_party/`, and `validators/` |
| Filesystem shape | Authored entries are regular files and directories. Symbolic links, special files, hidden paths, and materialized answer paths are invalid. |
| Git metadata | The workspace's `.git` directory is outside the authored tree. |
| Native package derived trees | `test-data/` and `statement-build/` are never authored source. |

## Problem configuration

`config/problem.json` is a required UTF-8 JSON object with exactly these fields:

| Field | Allowed values | Default |
| --- | --- | --- |
| `time_limit_ms` | 100 through 30000 | 2000 |
| `memory_limit_mb` | 1 through 2048 | 1024 |
| `mode` | `pass-fail` or `interactive` | `pass-fail` |
| `pass_limit` | 1 through 64 | 1 |

The service writes these defaults when it creates a new problem. Strict source consumers reject a missing field, an authored value outside its range, or a missing or malformed file; they do not clamp or guess. Authoring pages show configuration errors under Review and Publish and use defaults only to keep the editor operable. Saving the General form writes one complete canonical object. Verification, package export, contest package downloads, and native package construction reject invalid source at their entrance. Execution dispatches accepted values without another memory floor.

`config/build.json` is a required UTF-8 JSON object. Its fields and constraints are:

| Field | Type and location | Semantics |
| --- | --- | --- |
| `accepted_solution_source` | Optional source path under `solutions/` | Selects the main-correct solution. |
| `validator_source` | Optional C++ source path under `validators/` | Selects the generator output checker. |
| `checker_source` | Optional C++ source path under `checkers/` | Selects the pass-fail checker. |
| `interactor_source` | Optional C++ source path under `interactors/` | Selects the interactive interactor. |
| `generator_sources` | Optional array of unique source paths under `generators/` | Defines the generator allowlist; an absent field means an empty allowlist. |

All paths are normalized relative POSIX paths. An absent component selection means that component is not selected; runtime code does not scan a directory or infer a conventional filename. Source-mode constraints are:

| Problem mode | Required restriction |
| --- | --- |
| `interactive` | A checker selection is rejected. |
| `pass-fail` | An interactor selection is rejected. |
| External import | An adapter may infer a best-effort selection, but must write it into `config/build.json` before authored-source consumers run. |

Saving a checker, validator, interactor, or generator validates write access, the normalized component path, the UTF-8 source-size limit, and the resulting source selection before persisting the edit. Saving does not compile the source or contact a judgehost. Syntax and toolchain errors are reported by verification, package construction, or an explicit compatibility check that actually requests compilation.

Unknown fields, malformed JSON, invalid paths, and missing selected files are diagnosed without guessing a replacement. A missing `accepted_solution_source` is never inferred from a filename, list order, or descriptor.

## Test specification

`tests/spec.json` is a required UTF-8 JSON object whose only field is `tests`. Array order is testcase order. Each entry has this shape:

| Field | Required | Type and semantics |
| --- | --- | --- |
| `id` | Yes | Unique string of 3 through 12 decimal digits. |
| `kind` | Yes | `manual` or `gen`. |
| `sample` | No | Boolean; absent means `false`. |
| `sample_input` | No | Inline sample input string. |
| `sample_output` | No | Inline sample output string. |
| `sample_output_validate` | No | Boolean; absent means `true`. |
| `sample_json` | No | Structured `pair` or `interaction` sample object; mutually exclusive with `sample_input` and `sample_output`. |

Pair samples contain one or more contiguous passes with inline `input` and `output` strings. Interaction samples contain contiguous passes whose ordered events each provide `source` (`interactor` or `solution`) and inline `content`. Pass numbers start at one and are contiguous; event array order is the displayed protocol order. The Tests page validates this strict shape before submission, and unknown fields remain invalid on the server.

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

For an interactive sample, `presentation` is `interaction` and each pass has an `events` array such as `{"source":"interactor","content":"1\n"}`. Unknown or duplicate object keys, a top-level array, string booleans, normalized aliases, duplicate IDs, and other scalar types are invalid.

Limits are applied independently at these boundaries:

| Limit | Scope | Default | Failure behavior |
| --- | --- | --- | --- |
| `TEXTAREA_MAX_BYTES` | Serialized `tests/spec.json` | 256 KiB | Reject the file. |
| `STATEMENT_SAMPLE_MAX_BYTES` | Each sample's combined input/output or all inline `sample_json` content | 32 KiB | Reject the sample rather than truncate it. |

Different sample entries do not share another aggregate budget. The serialized-file limit remains their common envelope. The same per-sample limit applies when import, preview, or package rendering fills missing display data from materialized test input and answer files.

Judge input is not embedded in a test entry:

| Test kind | Runtime payload |
| --- | --- |
| `manual` | `tests/manual/<id>.in` |
| `gen` | `tests/generator/<id>.in` interpreted as a shell-word command. Its first token resolves a source selected by `generator_sources`; remaining tokens are its arguments. |

A missing payload, unselected or missing generator, or ambiguous generator token invalidates the source tree. Files merely present below `generators/` are not executable inputs until selected. An empty `tests` array has no implicit discovery behavior; verification and native package construction require at least one explicit test.

The runtime generator input payload is the generator executable invocation plus its command parameters. Its execution identity and scheduling semantics are defined by the [execution protocol](execution.md). Configured source programs live under established roots such as `generators/`, `validators/`, `checkers/`, and `solutions/`; the generator's configured output-checking component is selected from `validators/`.

## Solutions

Solution programs live directly below `solutions/`. The UI currently lists `.cpp`, `.cc`, `.cxx`, `.c++`, `.py`, and `.java` files; `.c` solution sources are not accepted or dispatched. Optional metadata is stored next to a source as `<source>.desc`.

| Descriptor rule | Contract |
| --- | --- |
| Canonical output | `expected: <behavior>` followed by zero or more `note: <text>` lines. |
| Omitted descriptor | Means `unknown`. An `unknown` descriptor with no note is omitted by the canonical writer. |
| Required key | `expected` occurs exactly once. |
| Allowed lines | Each non-empty line is `expected: ...` or `note: ...`. `behavior:`, `verdict:`, and unkeyed lines are invalid. |
| Main-correct selection | `accepted_solution_source` is the sole selection and has effective behavior `accepted`, whether or not it has a descriptor. Selecting it does not create or rewrite a descriptor. |

Current behavior values are:

| Behavior | Meaning |
| --- | --- |
| `accepted` | Expected accepted result. |
| `wrong_answer` | Expected wrong answer. |
| `tle_or_correct` | Expected timeout or accepted result. |
| `tle_or_re` | Expected timeout or runtime error. |
| `time_limit_exceeded` | Expected timeout. |
| `run_time_error` | Expected runtime error. |
| `compile_error` | Expected compilation error. |
| `rejected` | Expected rejected result. |
| `unknown` | No expected behavior is declared. |

No filename or list-order inference is performed at runtime. Polygon and ICPC importers may infer external intent and materialize descriptors for other behaviors before returning the imported workspace.

## Statements

Statement source languages are directories under `statement-sections/`. A language directory MUST exist to select that language; individual section files may be absent or empty.

| Section file | Rendered role |
| --- | --- |
| `name.tex` | Problem name. Missing or empty content falls back to the problem slug/title chosen by the current statement context. |
| `legend.tex` | Problem legend. |
| `input.tex` | Input section. |
| `output.tex` | Output section. |
| `interaction.tex` | Interaction section. |
| `notes.tex` | Notes section. |

Missing or empty `name.tex` uses the fallback above. Other missing sections render as empty content. The current renderer ignores `scoring.tex`.

Shared statement assets live under `statement-assets/`. The editable rendering sources are `statement/statements.ftl`, `statement/problem.tex`, and `statement/olymp.sty`. `statement/examples.tex` is optional editable source shared by every statement language: it is absent by default, the renderer uses the canonical examples template when it is missing, creating it opts into a repository-owned override, deleting it restores the canonical fallback, and an authored empty file deliberately renders an empty examples companion.

The following files are regenerated products:

| Path | Lifecycle |
| --- | --- |
| `statement/main.tex` | Regenerated; not authored source. |
| `statement/rendered/` | Regenerated render tree; not authored source. |
| PDFs | Regenerated output; not authored source. |
| `statement/rendered/<language>/examples.tex` | Derived examples companion written for every language render. |

The default `problem.tex` inputs the rendered companion. Both FTL templates receive the same render context. A custom problem template must input `examples.tex` to use the canonical or authored companion.

Statement languages are ordered as English, Chinese, then alphabetically. Sample order and explicit display overrides come from `tests/spec.json`.

The canonical examples template preserves Polygon compatibility by rendering `problem.sampleTests[].inputFile` and `.outputFile` through `\exmpfile`. It also accepts an optional structured extension at `problem.examples.samples`:

| Structured field | Values or contents |
| --- | --- |
| `samples[].number` | Sample number. |
| `samples[].presentation` | `pair` or `interaction`. |
| `samples[].passes[].number` | Pass number. |
| Pair pass | `inputFile`, `outputFile`. |
| Interaction pass | `events[].source = interactor \| solution`, `events[].textFile`. |

The structured extension uses controlled resource paths and never modifies authored source. It is derived from inline `sample_json` or canonical main-correct pass evidence. Event order and pass numbers are explicit; no alternation, event kind, or EOF entry is inferred. An explicitly empty `samples` array is authoritative.

## Publication

Publishing refuses a workspace based on an older published revision, commits the workspace, and pushes `main`. If the push fails, it attempts to roll back the new local commit. Verification signatures use the relevant source paths and canonical configuration. Verification cache payloads and derived packages do not replace Git provenance: their rows retain the source commit they were built from.

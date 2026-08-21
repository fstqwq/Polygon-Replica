# Package protocol

The package lifecycle is one-way:

```text
published source -> native package -> external packages
```

A native package materializes one immutable published Git revision with its committed source, generated testcase inputs, and official answers. Package export creates it; matching evidence from a full verification certifies it. External package adapters derive delivery formats from the same archive.

## Import boundary

Package archives are untrusted input. Importers open only format-selected members with normalized relative paths. Selected members must be regular, unencrypted files; malformed archives, duplicate or conflicting paths, traversal, symlinks, and special files are rejected. ZIP64 is accepted within the configured limits. Upload, entry, metadata, and expanded-content limits are defined by the [configuration contract](../operations/configuration.md).

| Format | Detection marker | Import result |
| --- | --- | --- |
| Polygon | `problem.xml` | Canonical workspace source |
| ICPC 2025-09 or supported DOMjudge layout | `problem.yaml` | Canonical workspace source |
| Native package | `config/problem.json` | Canonical authored source without package identity or certification |

Import writes canonical workspace source and discards the uploaded archive after conversion. Unknown members are not opened by an importer. Polygon resource discovery copies an optional `examples.tex` from `files/examples.tex` or its declared resource path to `statement/examples.tex`; otherwise the canonical renderer fallback remains in effect.

| Target | Import behavior |
| --- | --- |
| New problem | Create canonical source, commit it, and push `main`. |
| Existing problem | Overwrite converted paths, retain other paths, and leave the changes uncommitted. |

A native package importer selects only canonical authored roots. `test-data/`, `statement-build/`, and unknown top-level members remain unopened. Generated payloads and certification are not imported; the destination problem creates its own native package.

The ICPC importer accepts standard 2025-09 packages and the supported DOMjudge-compatible layout. It converts problem types, statements, validators, verdict directories, `submissions.yaml`, and DOMjudge expected-result annotations into the canonical workspace shape.

## Native package identity and contents

A native package belongs to one immutable published problem revision, and each problem/revision has at most one native package. Matching evidence from a full verification certifies it; otherwise it remains available as `not verified`. Certification does not change the archive.

The archive contains the published source in its canonical layout and adds two derived trees:

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

Only applicable members are present. Interactive tests may omit `answer`, and sample display files occur only when they differ from judge input or answer. Each authored statement language has a complete TeX working directory under `statement-build/`, including its rendered samples and required assets. Both derived trees are excluded from the source digest.

The manifest contains `source_commit`, `revision_number`, `source_digest`, `mode`, `pass_limit`, stable authored solution metadata, and the ordered tests. Each test records its identity, source kind, sample flag, and descriptors for its available payloads. A descriptor contains a canonical path, SHA-256, and size. Verification IDs, observed verdicts, and solution verdict summaries are not archive content. Readers require the current shape and ignore unknown fields.

Construction validates the source tree, manifest, declared payloads, paths, checksums, and complete inventory. A consumer verifies the recorded archive SHA-256, extracts members safely, and validates the manifest shape it needs.

A native package remains usable after `main` advances. A missing or corrupt archive makes it unavailable without changing the Git revision.

## Package export

Package export is the only workflow that creates a native package. A request freezes the current published `main` commit, and only one creation flow may run for the same problem and commit at a time.

| Request | Behavior |
| --- | --- |
| Default | Reuse a certified native package, or run full verification before creating or certifying one. |
| `Run standard solution only` | Reuse any available native package, or run input generation and the main correct solution to create one marked `not verified`. |
| External package | Prepare or reuse the native package under the selected verification mode, then run or reuse the selected adapter. |

A later successful full verification certifies an existing `not verified` package only when its inputs and answers match. Certification does not rewrite the native archive or cached external packages; evidence mismatch leaves the package unchanged and reports an error. External adapters may consume an available `not verified` package.

Package export always targets the published revision frozen at admission. Historical native packages remain downloadable but cannot be selected as new export inputs.

## External package formats

The ICPC and DOMjudge adapters provide executable `build` and `run` files for C++ validators and interactors. `build` defines `DOMJUDGE` and compiles the copied source for `run`. C component sources are unsupported.

### ICPC Problem Package 2025-09

The `icpc-2025-09` adapter emits the supported subset of ICPC Problem Package 2025-09.

| Area | Contract |
| --- | --- |
| Metadata | `problem.yaml` contains `problem_format_version: 2025-09`. Ordinary problems use scalar `pass-fail`; interactive or multi-pass problems use the corresponding YAML type sequence. |
| Statements | Language PDFs are stored below `statement/`. |
| Tests | Testcase pairs are stored below `data/sample/` and `data/secret/`. An interactive test without a canonical answer receives an empty `.ans`; an interactive or multi-pass sample without standard interaction data remains secret. |
| Programs | Validators use `input_validators/`; a checker or interactor uses `output_validator/`. |
| Submissions | Sources are stored below `submissions/`, and expected behavior is recorded in `submissions/submissions.yaml` without modifying source bytes. Submissions authored as compile errors are omitted with a warning. |
| Attachments | Authored attachments are copied into the package. |
| Excluded | `domjudge-problem.ini` and `problem_statement/`. |

### DOMjudge package

The `domjudge` adapter emits the DOMjudge package layout.

| Area | Contract |
| --- | --- |
| Identity | `domjudge-problem.ini` contains the requested short name. Standalone export uses the public slug segment; a contest download uses the problem index. |
| Metadata | `problem.yaml` uses DOMjudge-compatible scalar problem type and validation metadata. |
| Statement | The selected PDF is stored as `problem_statement/problem.pdf`. |
| Tests and programs | Testcase data, validators, checker or interactor, and authored attachments use the DOMjudge layout. |
| Submissions | Standard accepted, wrong-answer, time-limit, and runtime-error submissions use their conventional directories. Mixed expected behaviors use a language-appropriate `@EXPECTED_RESULTS@` source annotation. |
| Excluded | `statement/` and `submissions/submissions.yaml`. |
| Balloon color | Standalone export selects a stable color from the problem external ID. Contest export assigns colors by canonical roster ordinal and repeats after 18 problems. |

The contest palette is, in order: `#e6194b` (red), `#4363d8` (blue), `#ffe119` (yellow), `#3cb44b` (green), `#f58231` (orange), `#6b2c91` (purple), `#eeeeee` (white), `#9a6324` (brown), `#46d9e6` (cyan), `#303030` (black), `#ff6f91` (pink), `#9bdc28` (lime), `#9e9e9e` (silver), `#008080` (teal), `#d4a017` (gold), `#800000` (burgundy), `#aaffc3` (mint), and `#ffd8b1` (peach).

### QOJ package

The `qoj` adapter emits the source data archive consumed by QOJ's **Sync Test Data** operation. Members are stored at the ZIP root.

| Area | Contract |
| --- | --- |
| Tests | Ordered tests use `1.in`, `1.ans`, and so on. Sample display payloads are repeated as `ex_1.in`, `ex_1.ans`, and so on. An interactive test without a canonical answer receives an empty `.ans`. |
| Configuration | `problem.conf` selects the built-in judger, one 100-point subtask, and the published limits. Memory above QOJ's 6144 MiB ceiling and `pass_limit > 2` are rejected. |
| Modes | Two-pass output uses `polygon_runtwice`. Interactive two-pass output also uses `polygon_runtwice_interactive` and `interactor_run_type default`. |
| Checker | Exact vendored `ncmp.cpp`, `wcmp.cpp`, and `fcmp.cpp` use the corresponding built-in checker. Custom source becomes `chk.cpp`; a missing pass-fail checker receives a byte-exact `chk.cpp`. Interactive packages use `irscmp`. |
| Programs | The accepted solution, validator, and interactor are copied as `std.<extension>`, `val.cpp`, and `interactor.cpp`. Missing `std` or `val` warns that Hack must be disabled. QOJ Sync compiles these sources and validates the data. |
| Statement | `statement.pdf` uses English when available, otherwise the first authored language. |
| Attachments | Participant files retain their relative paths below `download/`. QOJ Sync creates the contestant download archive. |
| Unsupported | Custom judgers or managers, `require/`, communication mode, submit-answer mode, and custom scoring or subtask dependencies. |

This subset follows the [QOJ problem-data guide](https://qoj.ac/blog/qingyu/blog/1423).

### Nowcoder package

The `nowcoder` adapter emits the Nowcoder testcase archive.

| Area | Contract |
| --- | --- |
| Supported problems | Pass-fail with `pass_limit=1`. |
| Tests | Ordered pairs are stored at the ZIP root as `1.in`, `1.ans`, `2.in`, `2.ans`, and so on. |
| Checker | Custom checker source is copied to `checker.cc`; the adapter does not compile it. |
| Compatibility warning | Nowcoder uses an older `testlib.h` with C++14. A literal `setTestCase` reference produces an advisory warning but does not block publication. |

## Contest package bundle

A contest bundle requires an available native package for the current published revision of every roster problem. It applies one selected external adapter to each problem in canonical `idx` order. A missing or unavailable package rejects the request; the bundle does not select an older revision or start verification. Failure of any child package aborts the entire bundle.

| Area | Contract |
| --- | --- |
| Children | `packages/<idx>-<problem>.zip`, one per roster problem. |
| Placement | DOMjudge receives the contest `idx` and canonical ordinal for its short name and balloon color. ICPC, QOJ, and Nowcoder output is independent of contest placement. |
| Outer archive | Contains only the child packages and no manifest, Git commit, native package identity, or checksum metadata. |

## Contest import

| Limit | Contract |
| --- | --- |
| Problems | `CONTEST_MAX_PROBLEMS`; default 26, configured range 1-64. |
| Outer entries | `CONTEST_MAX_PROBLEMS x 4096`. |
| Outer expanded content | `CONTEST_MAX_PROBLEMS x PROBLEM_ZIP_MAX_EXPANDED_BYTES`. |
| Child archive | Must independently satisfy the single-problem entry and expanded-content limits. |

Imported problem indices must be unique within the contest.

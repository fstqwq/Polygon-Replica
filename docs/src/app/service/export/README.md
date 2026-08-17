# `app/service/export`

Owns Package Export jobs, derived-package cache lookup and publication, and the
package adapter registry. A request freezes the current published commit. Its worker
prepares or reuses that commit's Native Package. A Native request ends there;
it creates no `exports` row or second archive. A `domjudge`, `icpc-2025-09`,
`qoj`, or `nowcoder` request then runs the requested adapter or reuses its cached
external package. A separate request keeps a separate job ID even when it
resolves to the same cached archive.

Only one Package Export for a problem/commit is admitted at a time. Jobs expose
the phases `queued`, `verifying`, `packaging`, and `complete`; a Native job skips
`packaging`. Interrupted jobs become failed at startup. Missing or mismatched
derived-package bytes invalidate only their cache row. Corruption in the
underlying Native Package is handled by the problem-package workflow before
an adapter runs.

`adapters/` contains one module per external package format and the single
authoritative registry. Callers enumerate `PackageAdapterRegistry.adapters` or
`formats` and resolve a format with `require()`; they do not maintain format
string lists of their own. Each adapter owns the policy and output layout of
exactly one external format; shared filesystem mechanics live in the same package.
They receive an already validated `NativePackageReader`, canonical naming
options, and a caller-owned empty staging directory. They may render
statements, but do not access SQLite, Git, workspaces, verification rows,
runtime cache, or another adapter output. They do not create jobs or publish
archives. Both single-problem Export and Contest builds invoke this boundary
and own their respective atomic publication.

Adapters never execute authored source or start a compiler in the application
process or the local bubblewrap sandbox. An adapter that needs a source
compatibility result receives it through the Judgehost compile-only boundary.
There is no local compiler fallback when Judgehost is unavailable. The
Nowcoder uses an older `testlib.h` and a C++14 compiler, but the adapter does
not claim broad toolchain compatibility. It only warns on the literal
`setTestCase`, which the project checker guideline recommends but the older
Nowcoder testlib may not support, and does not compile the checker.

QOJ output is a source data archive for the target system's Sync Test Data
operation. The adapter writes testcase pairs, supported built-in-checker
selection, source programs, one preferred statement PDF, and participant files
below `download/`. QOJ Sync owns program compilation and generation of the
contestant download archive, together with target-side testcase validation and
normalization. The supported layout and execution-mode subset are defined by
the package protocol.

The Native Package is downloaded directly from package history. The Packages
page may submit a Native job to prepare a missing current Native Package, while
the Agent Package Export API continues to expose only external formats. Format
layouts, cache identity, and failure behavior are defined by the
[package protocol](../../../../protocol/package.md).

# Product scope and rationale

Polygon Replica covers the part of problem setting that begins after someone
has written an initial solution and a few tests. It provides the execution,
collaboration, and delivery workflow needed to turn that starting point into a
contest-ready problem.

## A complete problem-setting system

A useful problem-authoring system has three layers.

### Execution tools

The most visible layer compiles statements and programs, runs generators and
input validators, checks answers with testlib checkers or interactors, verifies
many solutions in parallel, and packages the result. Most command-line
problem-setting tools concentrate on this layer.

### Collaboration

A team also needs users, isolated workspaces, an agreed official version of
each problem, access control, review between authors, repeatable verification
results, contest organization, and a record of generated packages and PDFs.

Without this layer, teams reconstruct the workflow from Git, shell scripts,
shared folders, chat, and unwritten conventions. A capable local build tool can
therefore improve an individual task without reducing the coordination cost of
producing a complete problem set.

### Ecosystem contracts

Codeforces Polygon is useful not only because it executes programs, but because
its source layout, packages, solution expectations, and failure modes are
already familiar to problem setters and surrounding tools. Contest delivery
has similar contracts around ICPC Problem Packages and
[DOMjudge](https://www.domjudge.org/).

Polygon Replica brings these three layers into a self-hosted workflow. It
imports established package formats where supported, provides workspaces and
review, verifies problems on DOMjudge Judgehosts, and produces packages and
Contest outputs for downstream infrastructure. Its
[Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) companion adds an
Agent CLI so people and coding agents can operate the same workflow.

Polygon Replica is not a drop-in clone of the hosted Polygon service and does
not implement its private API. It is intended for teams that want to operate
the complete workflow themselves and are prepared to run the application,
persistent storage, and Judgehosts.

# `app/service`

Services own reusable domain behavior and infrastructure adapters. Their inputs
are canonical values prepared at HTTP/archive boundaries; their outputs are
domain records, locators, and execution results consumed by `app/impl`. They may
use SQLite, Git, filesystem, sandbox, and process mechanisms through the current
composition, but do not depend on templates or route registration.

Package delivery follows the service boundary `published source -> Native
Package -> external packages`. `problem_package` owns Native Package
materialization and the internal `NativePackageReader`; `export` owns the
external-package adapters and Package Export orchestration; and `contest`
consumes frozen readers without starting Verification or routing through
problem-level exports.

Current packages:

- [access](access/README.md), [agent](agent/README.md), [auth](auth/README.md), [contest](contest/README.md)
- [disk](disk/README.md), [repository](repository/README.md), [workspace](workspace/README.md)
- [problem](problem/README.md), [statement](statement/README.md), [execution](execution/README.md)
- [verification](verification/README.md)
- [judgehost](judgehost/README.md), [sandbox](sandbox/README.md)
- [problem_package](problem_package/README.md), [export](export/README.md), [importing](importing/README.md)
- [platform](platform/README.md), [runtime](runtime/README.md), [mail](mail/README.md)

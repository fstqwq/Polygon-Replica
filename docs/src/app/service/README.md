# `app/service`

Services own reusable domain behavior and infrastructure adapters. They may use
SQLite, Git, filesystem, sandbox, and process mechanisms through the current
composition, but do not depend on templates or route registration.

Current packages:

- [agent](agent/README.md), [auth](auth/README.md), [contest](contest/README.md)
- [disk](disk/README.md), [repository](repository/README.md), [workspace](workspace/README.md)
- [problem](problem/README.md), [statement](statement/README.md), [verification](verification/README.md)
- [judgehost](judgehost/README.md), [run](run/README.md), [sandbox](sandbox/README.md)
- [problem_package](problem_package/README.md), [export](export/README.md), [importing](importing/README.md)
- [platform](platform/README.md), [runtime](runtime/README.md), [mail](mail/README.md)

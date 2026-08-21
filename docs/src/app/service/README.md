# `app/service`

Services own reusable domain behavior and infrastructure adapters. They consume canonical boundary values and return domain records, locators, execution results, and read models. They do not depend on templates or route registration.

Package delivery follows `published source -> native package -> external packages`. `problem_package` owns native packages, `export` owns adapters and export orchestration, and `contest` builds request-scoped bundles from available native packages.

Current packages:

- [access](access/README.md), [agent](agent/README.md), [auth](auth/README.md), [contest](contest/README.md)
- [disk](disk/README.md), [repository](repository/README.md), [workspace](workspace/README.md)
- [problem](problem/README.md), [statement](statement/README.md), [execution](execution/README.md)
- [verification](verification/README.md)
- [judgehost](judgehost/README.md), [sandbox](sandbox/README.md)
- [problem_package](problem_package/README.md), [export](export/README.md), [importing](importing/README.md)
- [platform](platform/README.md), [runtime](runtime/README.md), [mail](mail/README.md)

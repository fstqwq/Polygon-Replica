# `app/service/repository`

Owns problem bare repositories and Git publication/workspace operations. It
creates repositories, resolves published commits, inspects history, and applies
the current publish/update workflow.

Git subprocess and path mechanisms are supplied by platform services. Durable
problem/workspace identities remain in SQLite.

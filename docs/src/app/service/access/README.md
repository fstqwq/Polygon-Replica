# `app/service/access`

Owns cross-resource authorization decisions. It combines system administrator
status, direct problem ACL, contest membership, workspace ownership, package
job ownership, and verification ownership into typed capability decisions and
block reasons.

The user-visible role and capability semantics are owned by the
[access model](../../../../design/access.md).

The package combines persisted access facts with role and agent-scope policy and returns typed decisions. HTTP handlers translate those decisions without reconstructing roles or broadening declared agent scope.

Problem access derived from a contest never grants problem ownership. A problem
reader may view and rejudge a visible verification, but only the workspace that
owns that verification may cancel it. Successful derived packages are shared
with problem readers, while unfinished and failed jobs remain visible only to
their actor and problem managers.

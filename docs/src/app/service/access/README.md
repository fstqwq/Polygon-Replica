# `app/service/access`

Owns cross-resource authorization decisions. It evaluates system administrator
status, direct problem ACL, contest membership, workspace ownership, package
job ownership, and verification ownership as independent persisted facts, then
returns typed capability decisions and block reasons.

The user-visible role and capability semantics are owned by the
[access model](../../../../design/access.md).

The package combines persisted access facts with role and agent-scope policy and returns typed decisions. HTTP handlers translate those decisions without reconstructing roles or broadening declared agent scope. Access mutations recheck the actor and immutable targets inside the same transaction that changes an ACL.

Problem access comes only from direct problem ACL or system administrator
status; contest membership never contributes a problem role. A problem reader
may view and rejudge a visible verification, but only the workspace that owns
that verification may cancel it. Successful packages are shared with problem
readers, while unfinished and failed jobs remain visible only to their actor
and problem managers.

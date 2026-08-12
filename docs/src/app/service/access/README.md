# `app/service/access`

Owns cross-resource authorization decisions. It combines system administrator
status, direct problem ACL, contest membership, workspace ownership, package
job ownership, and verification ownership into typed capability decisions and
block reasons.

`model.py` defines actors, resources, capabilities, decisions, and UI-facing
contexts. `policy.py` contains pure role and agent-scope rules. `store.py` owns
the access-specific SQLite queries. `query.py` composes persisted facts with the
policy. HTTP handlers translate the returned decisions; they do not reconstruct
roles or broaden an agent token.

Problem access derived from a contest never grants problem ownership. A problem
reader may view and rejudge a visible verification, but only the workspace that
owns that verification may cancel it. Successful package artifacts are shared
with problem readers, while unfinished and failed jobs remain visible only to
their actor and problem managers.

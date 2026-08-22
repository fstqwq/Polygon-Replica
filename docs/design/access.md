# Access model

## Authority

`app.service.access` owns reusable cross-resource authorization. It reads identity, direct problem ACL, contest membership, workspace ownership, package job ownership, and verification ownership as persisted facts, then returns typed capabilities and denial reasons. HTTP code supplies the actor and target and applies that decision.

System administrator access is the strongest role. Otherwise, effective problem access is the stronger of a direct `repo_acl` role and access derived from every contest containing the problem. Contest `read` derives problem `read`; contest `write` or `owner` derives problem `write`. Derived access never grants problem ownership or management. Removing a member or roster entry removes that access on the next query.

## Resource capabilities

Problem readers can read the problem, view and rejudge visible verifications, and list or download successful packages. Problem writers can change source and create packages. Direct problem owners and system administrators can manage the problem and its ACL.

A workspace is readable and writable only by its owner, subject to that user's effective problem capability; system administrators retain the administrative override. Verification visibility also requires the record to belong to the requested problem. A problem reader can rejudge a visible verification into their current workspace. Only the owner of the workspace behind an active verification can cancel it; published or another user's verification is view-only.

Successful packages are shared with problem readers. A queued, running, or failed package job is visible only to its actor or a problem manager. Contest members receive `read`, `write`, or fixed `owner` capabilities. Roster management requires contest ownership or system administration, and adding a problem also requires direct management of that problem.

## Authentication boundaries

Browser sessions, agent identities, and judgehost credentials are separate actors. An agent session has a user-selected general scope and may hold independently expiring per-problem grants. Combined agent scopes are capped by the connected user's current problem role and cannot retain access after that user loses direct and contest-derived access. Contest discovery also requires general scope and current contest read access. Browser sudo is session-bound and never inherited by an agent. Judgehost authentication authorizes only the trusted execution protocol under `/api/v4/*`; it creates no user or problem role.

Admin pages require an authenticated system administrator. Every state-changing request below `/admin` must carry an `Origin` or `Referer` whose origin exactly matches the application origin; session authentication and cookie same-site policy do not replace this check.

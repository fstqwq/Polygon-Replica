# Access model

## Authority

`app.service.access` owns reusable cross-resource authorization. Its store reads
identity, direct problem ACL, contest membership, workspace ownership, package
job ownership, and verification ownership as persisted facts. Its policy maps
those facts to typed capabilities and denial reasons. HTTP code supplies the
authenticated actor and target identity, then translates the returned decision
to a response or disabled control; it does not reconstruct roles.

System administrator access is the strongest role. Otherwise, effective
problem access is the stronger of a direct `repo_acl` role and access derived
from every contest that contains the problem. Contest `read` derives problem
`read`; contest `write` or `owner` derives problem `write`. Derived access never
grants problem ownership or problem-management capability. Removing a member or
roster entry removes that derived access on the next query.

## Resource capabilities

Problem readers can read the problem, view and rejudge visible verifications,
and list or download successful packages. Problem writers can mutate source and
create packages. Direct problem owners and system administrators can manage the
problem and its ACL.

A workspace is readable and writable only by its owning user, subject to that
user's effective problem capability; system administrators retain the
administrative override. Verification visibility also requires the record to
belong to the requested problem. A problem reader can rejudge a visible
verification into the reader's current workspace. Only the workspace that owns
an active verification can cancel it; published or another user's verification
is view-only.

Successful package artifacts are shared with problem readers. A queued,
running, or failed package job is visible only to its actor or a problem
manager. Contest members receive `read`, `write`, or fixed `owner` capabilities;
roster management requires the contest owner or a system administrator, and
adding a problem also requires direct management of that problem.

## Authentication boundaries

Browser sessions, agent tokens, and Judgehost credentials are separate actors.
An agent token's declared scope is capped by the user's current problem role;
it cannot gain sudo or broaden access after ACL or contest membership is
removed. Browser sudo is session-bound and is never inherited by an agent.
Judgehost authentication authorizes only the trusted execution protocol under
`/api/v4/*`; it does not create a user or problem role.

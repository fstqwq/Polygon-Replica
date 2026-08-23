# Access model

## Authority

`app.service.access` owns reusable cross-resource authorization. It reads identity, direct problem ACL, contest membership, workspace ownership, package job ownership, and verification ownership as persisted facts, then returns typed capabilities and denial reasons. HTTP code supplies the actor and target and applies that decision.

System administrator access is the strongest role. Otherwise, effective problem access is the stronger of a direct `repo_acl` role and access derived from every contest containing the problem. Contest `read` derives problem `read`; contest `write` or `owner` derives problem `write`. Derived access never grants problem ownership or management. Removing a member or roster entry removes that access on the next query.

## Resource capabilities

Problem roles grant the following cumulative capabilities:

| Problem operation | `read` | `write` | direct `owner` |
| --- | --- | --- | --- |
| Read the problem; render HTML/PDF/LaTeX previews | Yes | Yes | Yes |
| View and rejudge visible verifications; run standard full or statement-sample verification in the actor's workspace | Yes | Yes | Yes |
| List and download successful packages | Yes | Yes | Yes |
| Change source; use Custom Run; create packages; certify matching full-verification evidence | No | Yes | Yes |
| Manage the direct Problem ACL | No | No | Yes |

System administrators pass every role-gated row. Preview and standard verification freeze or read actor-scoped source and record only cache or private execution evidence; they do not authorize publishing that evidence as shared native-package certification.

A workspace is readable and writable only by its owner, subject to that user's effective problem capability; system administrators retain the administrative override. Verification visibility also requires the record to belong to the requested problem. A problem reader can rejudge a visible verification into their current workspace, but only actors with package-create capability may let that job certify a shared native package. Custom Run remains a problem-write capability even when its selected targets happen to cover every solution and test. Only the owner of the workspace behind an active verification can cancel it; published or another user's verification is view-only.

Successful packages are shared with problem readers. A queued, running, or failed package job is visible only to its actor or a problem manager.

Contest roles grant the following cumulative capabilities:

| Contest operation | `read` | `write` | fixed `owner` |
| --- | --- | --- | --- |
| Read the Contest and download completed Contest packages | Yes | Yes | Yes |
| Edit Contest properties and statement sources; build packages | No | Yes | Yes |
| Edit roster problems through Contest-derived Problem write | No | Yes | Yes |
| Change problem indices and ordering | No | Yes | Yes |
| Remove any roster problem | No | Yes | Yes |
| Add a problem | No | Only with direct Problem `write` or `owner` | Only with direct Problem `write` or `owner` |
| Manage Contest membership (`Manage access`) | No | No | Yes |

System administrators pass every role-gated row and the direct-access check for adding a problem. Access derived from another Contest is not direct Problem access and therefore cannot authorize adding that problem. Adding a problem is a persistent roster change: later loss of the adding user's direct Problem role does not remove the problem from the Contest.

Contest readers may render HTML review and PDF preview only when they also have problem read access to every roster problem. These previews use the actor's own existing workspaces or ready native packages and write only actor-scoped cache. Contest source editing, native-package construction, and contest package bundles retain their existing write or build capabilities.

## Authentication boundaries

Browser sessions, agent identities, and judgehost credentials are separate actors. An agent session has a user-selected general scope and may hold independently expiring per-problem grants. Combined agent scopes are capped by the connected user's current problem role and cannot retain access after that user loses direct and contest-derived access. Contest discovery also requires general scope and current contest read access. Browser sudo is session-bound and never inherited by an agent. Judgehost authentication authorizes only the trusted execution protocol under `/api/v4/*`; it creates no user or problem role.

Admin pages require an authenticated system administrator. Every state-changing request below `/admin` must carry an `Origin` or `Referer` whose origin exactly matches the application origin; session authentication and cookie same-site policy do not replace this check.

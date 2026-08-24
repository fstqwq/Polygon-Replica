# Access model

## Authority

`app.service.access` owns reusable cross-resource authorization. It reads
identity, direct problem ACL, contest membership, workspace ownership, package
job ownership, and verification ownership as persisted facts, then returns
typed capabilities and denial reasons. HTTP code supplies the actor and target
and applies that decision.

System administrator access is the strongest role. Otherwise, problem access
comes only from the direct `repo_acl` row for that problem and user. Contest
membership never grants problem access. Contest roles and problem roles are
independent even when a problem appears in that contest.

Problem and contest owners are fixed recovery anchors. Ordinary access actions
cannot grant, replace, or revoke an owner role. Writers may manage roles for
other users but cannot assign themselves a different role or change the access
of a system administrator. A non-owner contest member may remove their own
membership regardless of whether their current role is `read` or `write`.

## Resource capabilities

Problem roles grant the following cumulative capabilities:

| Problem operation | `read` | `write` | `owner` |
| --- | --- | --- | --- |
| Read the problem; render HTML/PDF/LaTeX previews | Yes | Yes | Yes |
| View and rejudge visible verifications; run standard full or statement-sample verification in the actor's workspace | Yes | Yes | Yes |
| List and download successful packages | Yes | Yes | Yes |
| Change source; use Custom Run; create packages; certify matching full-verification evidence | No | Yes | Yes |
| Grant or revoke another user's direct problem `read` / `write` | No | Yes | Yes |
| Delete the problem | No | No | Yes |

System administrators pass every role-gated row. Preview and standard
verification freeze or read actor-scoped source and record only cache or private
execution evidence; they do not authorize publishing that evidence as shared
native-package certification.

A workspace is readable and writable only by its owner, subject to that user's
direct problem capability; system administrators retain the administrative
override. Verification visibility also requires the record to belong to the
requested problem. A problem reader can rejudge a visible verification into
their current workspace, but only actors with package-create capability may let
that job certify a shared native package. Custom Run remains a problem-write
capability even when its selected targets happen to cover every solution and
test. Only the owner of the workspace behind an active verification can cancel
it; published or another user's verification is view-only.

Successful packages are shared with problem readers. A queued, running, or
failed package job is visible only to its actor or a problem owner.

Contest roles grant the following cumulative capabilities:

| Contest operation | `read` | `write` | `owner` |
| --- | --- | --- | --- |
| Read the contest | Yes | Yes | Yes |
| Edit contest properties and statement sources | No | Yes | Yes |
| Build packages or bulk-edit limits | No | Directly writable problems only | Same |
| Add a problem | No | With direct problem `write` or `owner` | With direct problem `write` or `owner` |
| Remove or reorder roster problems | No | Yes | Yes |
| Grant or revoke another user's contest `read` / `write` | No | Yes | Yes |
| Exit the contest by removing your own membership | Yes | Yes | No |
| Manage a problem row in the contest access matrix | No | With direct problem `write` or `owner` | With direct problem `write` or `owner` |
| Review all statements or download a completed contest package | With direct problem `read` for every roster problem | Same | Same |

System administrators pass every contest role gate and direct problem check.
Adding a problem is a persistent roster change: later loss of the adding user's
direct problem role does not remove it. `contest_problems.added_by_user_id` is
audit information and never authorizes access.

The contest access matrix projects direct `repo_acl` rows for current contest
members. A matrix edit is global problem access, not a contest-scoped grant.
Removing a member, removing a problem, or deleting a contest never deletes those
direct ACL rows. Matrix saves recheck contest write, direct problem write,
membership, immutable targets, and the originally rendered role in one
transaction; any invalid or stale cell rejects the whole save.

No read path or membership action synthesizes missing problem ACL rows.
Upgrades do not backfill direct problem ACL from contest membership. A user who
previously relied only on contest membership has no problem access until an
authorized writer grants a direct role.

## Authentication boundaries

Browser sessions, agent identities, and judgehost credentials are separate
actors. An agent session has a user-selected general scope and may hold
independently expiring per-problem grants. Combined agent scopes are capped by
the connected user's current direct problem role and cannot retain access after
that role is removed. Contest discovery also requires general scope and current
contest read access; each problem snapshot still requires direct problem access.
Browser sudo is session-bound and never inherited by an agent. Judgehost
authentication authorizes only the trusted execution protocol under
`/api/v4/*`; it creates no user or problem role.

Admin pages require an authenticated system administrator. Every state-changing
request below `/admin` must carry an `Origin` or `Referer` whose origin
exactly matches the application origin; session authentication and cookie
same-site policy do not replace this check.

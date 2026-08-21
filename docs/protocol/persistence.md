# SQLite persistence

The canonical schema and required-object manifest live in `app/db.py`. SQLite stores identities, relationships, configuration, lifecycle state, summaries, and filesystem locators. Committed source and large payloads remain in their owning filesystem roots.

## Execution rows

| Table | Authority |
| --- | --- |
| `verifications` | Source context, kind, mode, pass limit, status, failure, and timestamps for full, sample, custom, and package verification. |
| `verification_tasks` | Complete task graph, terminal status, and canonical `result_json`. |
| `verification_task_artifacts` | Ownership and locator index for downloadable cache payloads. |
| `verification_task_diagnostics` | Bounded late-diagnostic snapshot for one task. |

Admission inserts a taskless `queued` parent. Activation atomically changes it to `running`, stores its detail, and installs the complete validated graph. A plan is never partially installed or replaced.

Task completion atomically records terminal status, canonical result, artifact ownership, generated input or accepted answer ownership, and the first failure reason. Hard failures also fail the parent and cancel remaining open tasks. Solution mismatches allow independent tasks to finish before the parent becomes `failed`. User cancellation changes the parent and every open task to `cancelled`.

The first terminal task decision wins. Duplicate or conflicting completion returns persisted state without adding locators or replacing failure evidence. Judgehost callbacks are acknowledged only after that result is durable. Startup recovery uses `failed` for interrupted work and aborts startup if durable recovery cannot be committed.

Late diagnostics are merged and deduplicated independently of task completion. They cannot update canonical result, status, artifact locators, parent status, or failure reason.

### Linearization guarantees

Activation, completion, cancellation, and sanity finalization serialize their compare-and-set transitions. Readers therefore observe either a complete running graph or the preceding taskless queued state, and one terminal decision for each task and parent. Process-local overlays are applied after one consistent SQLite read and never compete with persisted authority.

## Packages and previews

| Table | Authority |
| --- | --- |
| `problem_package_materializations` | One native package identity and archive locator per problem/source commit, plus its certification reference. |
| `exports` | Cached external package for one native package and target format. |
| `export_jobs` | Individual package export requests and terminal summaries. |
| `statement_previews` | User-scoped disposable HTML/PDF preview request and terminal summary. |

Native package downloads do not create export records. Contest package bundles create no durable build or artifact rows. Preview payloads live below the cache root and are invalidated at startup; renderer and tool versions are excluded from preview identity.

## Contest rows

Contest roster rows store `idx` as both display identity and canonical order. Each contest has unique problem and `idx` values. Statement source changes increment `source_generation`.

Editable metadata is stored as a sparse string mapping in `contest_properties`. Property keys match `[a-z][A-Za-z0-9_]{0,63}` and may carry one language suffix:

```text
<property>
<property>.<language>
```

Rendering resolves the language-specific key, then the base key. Empty optional values are absent rows. `title` is required. The default template also recognizes Boolean `insertBlankPage` and string `banner`; absent values resolve to false and empty string.

## Agent authorization rows

| Table | Authority |
| --- | --- |
| `agent_sessions` | Connected desktop session and its general `none`, `readonly`, `workspace`, or `commit` scope. |
| `agent_problem_grants` | Independently expiring or revoked per-problem scopes. |
| `agent_access_requests` | Expiring request whose approval creates one grant. |

Agents authenticate with a random `polygon_agent_...` bearer credential. SQLite stores only its SHA-256 verifier. The identity hash validates explicit reconnect metadata and is not a credential. Effective agent authority combines active declared scopes and remains capped by the connected user's current role.

## Configuration

`system_config` stores durable JSON overrides, update identity, and timestamp. The typed registry defines keys, defaults, validation, and restart behavior. SMTP configuration uses its dedicated singleton row.

## Schema changes

An empty database is initialized with the current schema. Existing databases are validated read-only before runtime starts. Missing required tables, columns, or named indexes produce a schema-blocked `503` and require an offline upgrade.

Startup never applies DDL to an existing database. Extra schema objects are preserved but do not satisfy current requirements. The deployment guide records the [latest breaking database commit](../operations/deployment.md#upgrade); an older deployment compares its schema with the target and applies the complete diff while the service is stopped.

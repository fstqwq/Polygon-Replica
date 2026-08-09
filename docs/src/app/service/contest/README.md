# `app/service/contest`

Owns contests, membership, problem roster, attachments, build jobs, frozen build
items, and contest artifacts. Contest definitions and attachments are durable;
derived build products follow artifact cleanup policy.

Build work is asynchronous through the shared worker queue. Some build policy
still lives in `app/impl/contest`; that placement is tracked in findings.

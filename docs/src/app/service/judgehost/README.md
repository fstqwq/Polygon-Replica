# `app/service/judgehost`

Adapts generic execution work to the DOMjudge-compatible judgehost surface. It
admits prepared cases, leases them to authenticated hosts, serves their files,
normalizes callbacks into structured execution results, records host and
toolchain telemetry, and cleans process-local runtime state.

Its inputs are prepared verification payloads, runtime blobs, and authenticated
`/api/v4/*` requests. Its outputs are work descriptions, file payloads,
terminal decisions, and late diagnostics. Durable publication crosses the
injected judgehost execution port; this package does not import verification
services or query verification tables.

The lifecycle is:

```text
admission -> lease -> callback normalization
          -> durable publication through the execution port -> cleanup
```

Case publication and batch closure have separate ownership. Publication takes
the pending cases under a short state lock, persists their results independently
of other cases in the batch, acknowledges them, and notifies coordination.
A callback awaiting durable acknowledgement waits for an existing publisher of
its selected cases, then acknowledges the stored result or takes over a failed
publication. Waiting releases the state lock; other cases remain independent.
Failed publication and late diagnostics use the pending-case index for retries.
The coordinator closes completed programs; the runtime closes a batch after its
cases are terminal and acknowledged and active publication has released them.

Task, batch, host, lease, callback-receipt, and cache-index state is
process-local and is reset at startup. Runtime source and evidence blobs are
stored below the disposable cache root. Missing final callbacks and compile
failures are converted into typed terminal outcomes before publication; they do
not create a second persistence boundary inside the judgehost service.

Prepared work keeps verification program identity separate from per-execution
run identity and content-addressed compile identity. The injected execution
port validates durable task bindings before cases become fetchable and before
lease, diagnostic, or completion events are published.

The case-result cache stores canonical immutable execution results in its existing
process-local index. Successful durable acknowledgement permits publication;
cancellation losers cannot publish. The first writer owns an entry. Each lookup
checks every referenced pass artifact before returning the stored object. Missing
artifacts invalidate the entry. Canonical validation checks usage types and nested
immutable diagnostics before insertion.

Compile callbacks decode and truncate output and metadata as bytes before deriving
failure diagnostics. Encoding happens when writing the existing base64 fields.
Download streams encode aligned memory views and retain at most two carry bytes.
The response closes its iterator and releases maintenance admission on completion,
stream failure, cancellation, or client disconnect.

The formal wire contract for ACK semantics, callback retry, lease deadlines,
cancellation, toolchain reports, and interactive or multi-pass evidence is
defined by the
[Judgehost protocol](../../../../protocol/judgehost.md).

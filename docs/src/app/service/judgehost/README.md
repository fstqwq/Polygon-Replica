# `app/service/judgehost`

Adapts generic execution work to the DOMjudge-compatible judgehost surface. It
admits prepared cases, leases them to authenticated hosts, serves their files,
normalizes callbacks into structured execution results, records host and
toolchain telemetry, and cleans process-local runtime state.

Its inputs are prepared Verification payloads, runtime blobs, and authenticated
`/api/v4/*` requests. Its outputs are work descriptions, file payloads,
terminal decisions, and late diagnostics. Durable publication crosses the
injected judgehost execution port; this package does not import Verification
services or query Verification tables.

The lifecycle is:

```text
admission -> lease -> callback normalization
          -> durable publication through the execution port -> cleanup
```

Task, batch, host, lease, callback-receipt, and cache-index state is
process-local and is reset at startup. Runtime source and evidence blobs are
stored below the disposable cache root. Missing final callbacks and compile
failures are converted into typed terminal outcomes before publication; they do
not create a second persistence boundary inside the judgehost service.

Prepared work keeps Verification program identity separate from per-execution
run identity and content-addressed compile identity. The injected execution
port validates durable task bindings before cases become fetchable and before
lease, diagnostic, or completion events are published.

The formal wire contract for ACK semantics, callback retry, lease deadlines,
cancellation, toolchain reports, and interactive or multi-pass evidence is
defined by the
[Judgehost protocol](../../../../protocol/judgehost.md).

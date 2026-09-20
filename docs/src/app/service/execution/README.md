# `app/service/execution`

Owns canonical execution results shared by judgehost, verification, and custom runs. It defines immutable outcome, compile, pass, resource, warning, and cache-reference values; validates complete ordered evidence; provides the strict persistence codec; and derives shared test/pass read models.

Verification identity, task storage, judgehost transport, HTTP, and runtime blob availability remain with their owning services. Cache references are evidence locators, not storage authority.

The codec accepts unknown incoming JSON values and checks their exact fields and
types. Outgoing outcome, compile, usage, pass and artifact objects have fixed
typed shapes. Extensible diagnostic fields use recursive JSON values; canonical
results freeze nested diagnostic arrays as tuples and objects as immutable
`CompileDiagnostic` mappings. Type annotations do not bypass canonical validation.

See the [execution protocol](../../../../protocol/execution.md) for lifecycle,
result, and availability semantics.

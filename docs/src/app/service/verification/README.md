# `app/service/verification`

Owns verification/custom-run summaries, selected tests and sources, signatures,
task DAG construction and scheduling, structured execution results, sanity
checks, result finalization, and artifact reference publication.

Generator parameters are part of the generator payload identity. The generator
checker performs validation; no standalone validator DAG task is inserted.
Judgehost execution is accessed through the current service composition.

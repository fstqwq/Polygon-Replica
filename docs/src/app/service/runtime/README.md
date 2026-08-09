# `app/service/runtime`

Owns runtime metadata and state-facing service helpers used by startup and admin
status. Application-wide dependency construction currently also occurs under an
implementation package; moving that composition boundary is tracked as PLC-001.

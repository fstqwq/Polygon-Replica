# `app/service/disk`

Provides concrete filesystem-backed stores for workspaces, verification data,
and related payloads. It validates and resolves paths below configured roots and
translates between service locators and files.

It does not define domain lifecycle or publication policy. Cross-store boundary
fragmentation is recorded as PLC-009.

# `app/service/problem_package`

Owns native package identity, materialization, certification, availability, and validated reading.

Inputs are one published problem revision and generated input/main-correct answer evidence. Output is one archive containing canonical source, ordered test payloads, manifest, and offline statement builds. Construction validates source, paths, checksums, and inventory before publication.

One native package exists per problem/source commit. Full verification may certify matching evidence without rewriting the archive. Missing or corrupt archives become unavailable; startup fails interrupted builds without opening every completed archive.

Package export may create a missing package. Adapters and contest bundles may only read an available package. The [package protocol](../../../../protocol/package.md) defines identity, archive contents, certification, and consumption.

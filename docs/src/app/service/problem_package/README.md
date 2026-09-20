# `app/service/problem_package`

Owns native package identity, materialization, certification, availability, and validated reading.

Inputs are one published problem revision and generated input/main-correct answer evidence. Output is one archive containing canonical source, ordered test payloads, manifest, and offline statement builds. Construction validates source, paths, checksums, and inventory before publication.

Manifest decoding validates nested solution, testcase, and file-descriptor shapes at the JSON boundary and returns typed records. File validation then checks those records against the authored source and actual payloads. Package workflow targets carry explicit path, expected behavior, and program identity; SQLite rows are projected into typed package/build records before leaving the store.

One native package exists per problem/source commit. Full verification may certify matching evidence without rewriting the archive. Missing or corrupt archives become unavailable; startup fails interrupted builds without opening every completed archive.

Package export may create a missing package. Adapters and contest downloads consume available native packages. The [package protocol](../../../../protocol/package.md) defines identity, archive contents, certification, and consumption.

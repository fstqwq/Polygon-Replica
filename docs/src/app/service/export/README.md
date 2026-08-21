# `app/service/export`

Owns package export requests, external-package cache, and the adapter registry.

A request freezes the current published commit, prepares or reuses its native package, and optionally builds one external format. Native export stops at the native package; external export runs or reuses the selected adapter. Only one export flow for a problem/commit is active at a time, and interrupted requests fail at startup.

The registry is the sole list of external formats. Each adapter owns one target layout and receives an available native package plus canonical naming and optional contest placement. Single-problem export publishes reusable external archives; contest export creates transient child packages.

The [package protocol](../../../../protocol/package.md) defines supported formats, verification modes, cache identity, archive contents, and failure behavior.

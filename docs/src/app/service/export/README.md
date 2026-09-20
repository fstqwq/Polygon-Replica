# `app/service/export`

Owns package export requests, external-package cache, and the adapter registry.

A single-problem request freezes the current published commit, prepares or reuses its native package, and optionally builds one external format. Native export stops at the native package; external export runs or reuses the selected adapter. Export coordination uses the problem, commit, and format, and interrupted requests fail at startup.

The registry is the sole list of external formats. Each adapter owns one target layout and receives an available native package plus canonical naming. Single-problem and contest downloads share the same reusable external archives. Contest assembly rewrites DOMjudge placement in a transient child package and copies the other formats unchanged.

The [package protocol](../../../../protocol/package.md) defines supported formats, verification modes, cache identity, archive contents, and failure behavior.

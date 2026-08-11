# `app/config`

Owns all admin-editable configuration definitions, scalar normalization,
cross-field validation, and the immutable active `ConfigValues` snapshot.
Definitions explicitly carry their category and restart behavior; SQLite stores
only non-default overrides through the platform configuration service.

This package does not own fixed protocol fields, paths, regular expressions,
templates, or enumerations. Those remain in their domain constant modules.

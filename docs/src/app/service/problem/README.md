# `app/service/problem`

Owns authored problem configuration, test specification parsing, content review,
problem metadata, and problem-level source interpretation. Git remains the
authority for committed files.

`tests/spec.json`, program roots, and workspace statement source are consumed in
the canonical shapes described by the problem-source protocol.

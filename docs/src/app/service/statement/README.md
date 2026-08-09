# `app/service/statement`

Owns statement source interpretation, language contexts, templates, signatures,
rendering, preview products, and statement archive support. It recognizes the
six canonical section files under each `statement-sections/<language>/`
directory and shared `statement-assets/`.

Editable sections are source; rendered TeX/PDF is derived and cleanup-safe.

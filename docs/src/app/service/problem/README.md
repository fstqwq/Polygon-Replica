# `app/service/problem`

Owns interpretation of authored problem source: build configuration, runtime limits, test specification, solution metadata, content review, readiness, and UI read models.

Strict source codecs protect verification and export admission. Authoring read models keep malformed or incomplete workspaces editable while reporting their errors. Git publication, execution, and derived package production belong to other services.

Readiness compares the current workspace or published commit with visible verification and package records without treating record ownership as source equivalence. Request-scoped projections derive shared page metadata once for headers, navigation, sidebars, and contest problem rows.

The [problem source protocol](../../../../protocol/problem-source.md) defines canonical files, limits, defaults, and publication behavior.

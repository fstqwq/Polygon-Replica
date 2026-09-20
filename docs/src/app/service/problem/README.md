# `app/service/problem`

Owns interpretation of authored problem source: build configuration, runtime limits, test specification, solution metadata, content review, readiness, and UI read models.

Strict source codecs protect verification, import, and package workflows. Authoring read models return configuration diagnostics and normalized page inputs, including safe repair of recognized obsolete build fields. Component projections supply file availability and generator usage. Git publication, execution, and derived package production belong to other services.

Generator resolution builds an alias index from the selected source list once per operation. Repeated testcase lookups share the index during that operation.

Authoring configuration carries parsed testcase entries and their diagnostic
through page assembly. Test editor and Run selectors project these entries;
solution selectors also reuse the component's existing source rows. Editor row
limits apply to payload display. The generator-script reader holds the workspace
lock while reading its own spec and every declared generator command, keeping
the script consistent with concurrent testcase replacement. Run New uses the
authoring projection for its choices; submitted work loads strict configuration.

Single-file Java entry-point detection is shared source interpretation used by judgehost preparation and external-package adapters.

Problem identifier normalization is shared by individual and Contest package
imports; each import resolves name collisions within its own operation.

Readiness compares the current workspace or published commit with visible verification and package records without treating record ownership as source equivalence. Request-scoped projections derive shared page metadata once for headers, navigation, sidebars, and contest problem rows.

The [problem source protocol](../../../../protocol/problem-source.md) defines canonical files, limits, defaults, and publication behavior.

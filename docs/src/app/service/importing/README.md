# `app/service/importing`

Owns bounded archive admission and conversion of Polygon, native package, ICPC, DOMjudge-compatible, and Polygon contest packages into canonical workspace source.

Importers validate archive structure and paths, charge selected members against expansion limits, and stream selected files into staging. Unknown payloads remain unopened. Native package import selects authored roots and excludes test data, offline statement builds, generated answers, and certification.

The caller owns Git and workspace integration. The [package protocol](../../../../protocol/package.md) defines formats, budgets, and merge behavior.

# Statement preview protocol

## Render source

Statement preview accepts the current user's workspace, the current ready native package, or an explicitly selected available historical `native_package_id` belonging to the problem. Every source produces the same render tree:

```text
source snapshot
  -> problem.tex + examples.tex + statement/sample resources
  -> HTML renderer or PDF compiler
```

Statement preview requires problem read access. Workspace source always means the requesting actor's own workspace and renders authored source with sample evidence; it never selects another user's checkout. Native package source reads a current ready package or an explicitly selected available historical package and never builds, rebuilds, or publishes one. Generated examples never modify authored source. The synchronous `statement_previews` flow is the only statement-preview lifecycle: it uses `sp-*` identities and actor-scoped cache payloads. Preview records, IDs, resources, and logs are scoped to the requesting user and remain subject to problem or contest access checks.

## HTML

```text
problem.tex
  -> expand bounded local \input files
  -> translate registered statement and sample macros
  -> Pandoc HTML5 + MathML
  -> rewrite local image resources
  -> sanitize
```

Includes and resources must remain inside the render tree. The renderer supports pair, multi-pass, interactive, and multi-pass interactive samples. Unknown macros produce visible warnings. PDF images use Poppler and SVG conversion uses librsvg. External URLs, traversal, unsafe raw HTML, scripts, event attributes, and dangerous URL schemes are rejected or removed; the output loads no external MathJax or CDN resource.

## Problem outputs

The Statement editor exposes `Preview: PDF HTML LaTeX` to problem readers. LaTeX opens rendered workspace source directly. HTML and PDF synchronously reuse a matching `statement_previews` cache entry or generate the result before returning it; there is no asynchronous workspace compile or status endpoint. Dynamic workspace samples may create foreground `sample` verification evidence in the actor's own workspace, but cannot certify a native package. Cache identity is checked before expensive source preparation and again before publication. Historical package links carry the exact native package identity.

## Contest review

Contest HTML review and PDF preview require contest read access plus problem read access for every roster problem. Workspace source uses only the requesting actor's existing, path-validated workspace for each problem; it never creates a missing workspace or falls back to an owner's checkout. Package source requires a ready available package for every problem and never starts a package build. A workspace or package link exists only when every problem has the requested source and the source languages have a non-empty intersection.

Contest review builds or reuses problem previews in canonical `idx` order and returns one complete document after all problems finish. A failed problem remains in place with its diagnostic and available log; successful statements remain visible. The combined HTML result is reusable while the ordered problem identities and statuses remain unchanged.

| Input | Resolution |
| --- | --- |
| Source | `workspace` or `native_package`. |
| Language | Explicit canonical language; otherwise English when available, then the first available language. |
| Property | `<key>.<language>`, then base `<key>`. Empty or absent overrides inherit the base value. |
| Resource | `statements/<language>/` overrides `statements/_shared/` at the same relative path. |

Effective properties are injected as FTL variables and through the `properties` mapping. `statements.ftl` and `olymp.sty` are language-specific entry files. Polygon contest import maps authored `statements.tex` to the internal `statements.ftl`; derived output keeps the `statements.tex` name.

## Contest PDF

Contest PDF preview renders `statements.ftl` with effective properties and ordered problem entries, then compiles the resulting `statements.tex` as one TeX document. `insertBlankPage=true` enables the template's blank-page signal, and `banner` fills the `\StatementBanner` slot. The build includes contest resources, all problem render trees, MetaPost preparation, and two XeLaTeX passes.

The synchronous GET request resolves the current contest roster and source
identity, then returns the matching cached or newly compiled PDF inline. There
is no POST build, preview-ID redirect, or separate contest PDF file-download
stage. `sp-*` remains an internal actor-scoped cache identity rather than a
public historical download locator. Problem and contest PDF failures use the
same diagnostic projection: a bounded excerpt beginning at the first LaTeX `!`
error, followed by the complete sanitized `latex.log`. Preview payloads and logs
remain cache.

Problem render-tree preparation and renderer failures produce a failed preview
record. Problem HTML and PDF endpoints return HTTP 422 with the recorded
diagnostic instead of exposing the failure as an unhandled server error.

## Identity and invalidation

A preview identity contains the subject, source, language, output kind, source revision or content, statement resources, sample evidence, and explicit output options. Tool, renderer, package, executable, container, and toolchain versions are excluded.

All preview records and payloads are disposable cache. Startup and renderer deployment invalidate successful and in-flight results. A missing payload is a cache miss.

## Sandbox limits

Pandoc, Lua, Poppler, librsvg, and TeX run through bubblewrap without network access. Source mounts are read-only, output is isolated, and CPU, memory, process, file-output, timeout, image, AST, HTML, and sample limits apply.

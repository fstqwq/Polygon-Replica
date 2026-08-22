# Statement preview protocol

## Render source

Statement preview accepts the current user's workspace, the current ready native package, or an explicitly selected available historical `native_package_id` belonging to the problem. Every source produces the same render tree:

```text
source snapshot
  -> problem.tex + examples.tex + statement/sample resources
  -> HTML renderer or PDF compiler
```

Workspace preview renders authored source with sample evidence. Native package preview reads `statement-build/<language>/`. Generated examples never modify authored source. The synchronous `statement_previews` flow is the only statement-preview lifecycle: it uses `sp-*` identities and actor-scoped cache payloads. Preview records, IDs, and resources are scoped to the requesting user and remain subject to problem or contest access checks.

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

The Statement editor exposes `Preview: PDF HTML LaTeX`. LaTeX opens rendered source directly. HTML and PDF synchronously reuse a matching `statement_previews` cache entry or generate the result before returning it; there is no asynchronous workspace compile or status endpoint. Cache identity is checked before expensive source preparation and again before publication. Historical package links carry the exact native package identity.

## Contest review

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

The request returns the cached or newly compiled PDF inline. Problem and contest PDF failures use the same diagnostic projection: a bounded excerpt beginning at the first LaTeX `!` error, followed by the complete sanitized `latex.log`. Preview payloads and logs remain cache.

## Identity and invalidation

A preview identity contains the subject, source, language, output kind, source revision or content, statement resources, sample evidence, and explicit output options. Tool, renderer, package, executable, container, and toolchain versions are excluded.

All preview records and payloads are disposable cache. Startup and renderer deployment invalidate successful and in-flight results. A missing payload is a cache miss.

## Sandbox limits

Pandoc, Lua, Poppler, librsvg, and TeX run through bubblewrap without network access. Source mounts are read-only, output is isolated, and CPU, memory, process, file-output, timeout, image, AST, HTML, and sample limits apply.

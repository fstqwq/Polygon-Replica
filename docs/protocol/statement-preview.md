# Statement preview protocol

## Render source

Statement preview accepts the current user's workspace, the current ready native package, or an explicitly selected available historical `native_package_id` belonging to the problem. Every source produces the same render tree:

```text
source snapshot
  -> problem.tex + examples.tex + statement/sample resources
  -> HTML renderer or PDF compiler
```

Statement preview requires problem read access. Workspace source always means the requesting actor's own workspace and renders authored source with sample evidence; it never selects another user's checkout. Native package source reads a current ready package or an explicitly selected available historical package and never builds, rebuilds, or publishes one. Generated examples never modify authored source. The synchronous `statement_previews` flow is the only statement-preview lifecycle: it uses `sp-*` identities and actor-scoped cache payloads. Preview records, IDs, resources, and logs are scoped to the requesting user and remain subject to problem or contest access checks.

## PDF language

The default `statements.ftl` selects the `olymp` Chinese option for the
`chinese` render language. It localizes problem and section headings, both
legacy and structured sample headings, interaction directions and blank-page
text. The default `problem.tex` localizes limit units. English rendering keeps
its existing wording. Existing authored templates remain unchanged; restore
`statements.ftl`, `problem.tex` and `olymp.sty` together to adopt these defaults.

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

Presentation conversion preserves standard TeX size declarations and size
environments (`tiny` through `Huge`), explicit `fontsize`/`selectfont`, nested
groups, paragraph alignment, and caption font sizes. Minipages retain their
width and vertical alignment in wrapping rows, with horizontal separators.
Wrapfigure environments with `r`/`R` or `l`/`L` placement retain their width,
images and scoped caption formatting as right or left floats. Browser text
flow determines the wrapped line count; optional TeX line-count and overhang
arguments do not control HTML layout. Narrow containers stack the figure above
its following text. Section headings and statement boundaries clear floats.
Dimensions use CSS units; the Polygon-specific `htmlPixelsInCm` macro does not
change their scale. HTML renderer revisions participate in preview cache identity.
Parboxes preserve their content and width within individual table cells.
Tables in centered environments are centered as a whole, while cell alignment
remains independent. Explicit `@{}` column spacing supports empty separators,
`quad`, `qquad`, and bounded `hspace` lengths. Relative image and parbox
dimensions use the enclosing statement or text box width, including inside
table cells; inline icons retain font-relative dimensions.
Sample text inherits the enclosing font scope. Verbatim text and mathematics
retain their source commands for their respective readers.

The HTML boundary accepts only bounded numeric dimensions and recognized
alignment values for presentation styles. It preserves image dimensions,
table alignment, and the supported MathML font, accent, and spacing attributes.
Boxed formulas use a bordered MathML Core node for browser rendering. All
other style declarations remain subject to removal.

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

Contest problem marks use `mark.<owner>/<slug>` keys in `contest_properties`.
The suffix follows the full problem ID rules; each key is an independent,
non-localizable plain-text string. Marks are excluded from effective statement
properties and FTL variables. Ordinary `mark` and `mark.<language>` properties
retain their language semantics, including when their group is deleted.
Contest readers see marks; contest writers edit them through the property save
endpoint. Blank values delete the key, and removing a roster entry preserves its
mark. Changes retain the normal property source-generation behavior.
`POST /contests/{contest}/properties/save` accepts `response_format=json` to
return `{"values": {"<submitted-key>": "<saved-value>"}}`, using an empty string
for deleted values. The default HTML response remains a redirect.

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

## Optional sample shading

Traditional single-pass samples use validator input markup and checker output
markup from successful execution evidence. The parser restores the marked bytes
and requires an exact match with the displayed UTF-8 sample. Case numbers are
positive and increasing; boundaries must identify whole lines (a token reader's
unread trailing line whitespace belongs to the preceding case). Odd case numbers
are shaded, and text before the first case stays plain. Invalid, missing,
oversized, or mismatched optional markup falls back to an unshaded sample.

Workspace preview sample verification also validates authored `sample_output`
when its validation is enabled and retains the checker markup for those exact
displayed bytes. Other sanity checks remain scoped to full verification.

The shared producer creates numeric `.shade` and `.shade.tex` sidecars beside
sample resources. HTML and PDF consume the same line selection, with colored
padding on the first and last line. Shaded PDF samples wrap long lines within
their cells. Native Package `statement-build` preserves these resources for
problem and Contest previews. Existing packages without sidecars render plain
samples. Renderer revisions participate in both HTML and PDF preview identity.

### Shading controls

Sample shading defaults to enabled. Contest property `shadeSamples` accepts
`true` or `false`, is non-localizable, and supplies the default for each Problem.
The Properties page offers **Remove sample shading** to writers when the property
is absent. The existing preset endpoint inserts `shadeSamples=false`; Edit changes
the value and Delete restores the enabled default. An existing value is preserved
when a stale preset request is rejected.

An authored `statement/examples.tex` can override the inherited value with a
literal top-level command before the sample environment:

```tex
\StatementSampleShading{false}
```

`true` enables shading. Other arguments produce a rendering error. Comments and
verbatim text do not set the control. Each Problem has independent state; its
setting ends with that Problem. Disabled samples use ordinary presentation while
retaining their markup artifacts and numeric sidecars. Preview identities include
the inherited HTML setting; Contest PDF identities include property generation.

Authored styles and templates remain unchanged during rendering. PDF controls
require the current `olymp.sty`. To upgrade a customized Contest template, retain
its layout and add this before the Problem import loop:

```tex
\ifdefined\StatementSampleShading
\StatementSampleShading{<#if shadeSamples??><#if shadeSamples>true<#else>false</#if><#else>true</#if>}
\fi
```

The current default template includes this block. Older PDF styles retain their
existing presentation. HTML controls are interpreted by the renderer independently
of the authored PDF style.

### Restoring Problem templates

The Statement page offers a **Restore defaults** dropdown only when managed
files differ from their defaults. Its checkboxes list differing `statements.ftl`,
`problem.tex`, `olymp.sty`, `examples.tex`, and `third_party/testlib/testlib.h`.
The POST to `/problems/{problem}/statement/templates/reset` accepts repeated
allowlisted `path` fields and validates all selected paths before writing.
Restoring `examples.tex` removes the optional override and selects the built-in
default. Confirmation applies to the selected replacements; unselected files,
statement sections, and attachments are preserved.

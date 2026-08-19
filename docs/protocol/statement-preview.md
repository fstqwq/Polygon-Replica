# Statement Preview protocol

## Render source

Statement Preview accepts either the current user's Workspace or the current
published Native Package. Both sources produce the same canonical Problem
render tree:

```text
source snapshot
  -> problem.tex + examples.tex + statement/sample resources
  -> HTML renderer or PDF compiler
```

Workspace preparation uses the same `StatementExamplesProducer` as package
materialization. Native Package preparation reads the already-rendered
`statement-build/<language>/` tree. Neither path writes derived examples back
to authored Problem source.

Every Preview record is scoped to the requesting user. Workspace-derived
content is never reused by another user's Preview request, and Preview IDs and
resources are only readable by that same user after the normal Problem or
Contest ACL check succeeds.

## HTML

The HTML renderer passes the rendered `problem.tex` to Pandoc's LaTeX reader.
Before translating project macros, the Lua filter expands rendered `\input`
commands recursively and in place. Includes are resolved from the render-tree
root, must remain inside that root, and are bounded by count and total bytes.
The filter does not treat `examples.tex` or any other filename as a private
insertion protocol: it translates the registered Polygon macros that occur in
the expanded source at their original position. This produces semantic sample
structures for legacy pairs, multipass pairs, interaction, and multipass
interaction. Unknown macros remain visible as best-effort warnings rather than
invoking a general TeX interpreter.

Pandoc emits HTML5 with MathML. A controlled resource pass copies ordinary
images and converts PDF images with Poppler; SVG is converted with librsvg when
needed. Resource paths must remain inside the render tree. External URLs,
absolute paths, traversal, unsafe raw HTML, scripts, event attributes, and
dangerous URL schemes are rejected or removed before the fragment is stored.
The UI uses no external MathJax or CDN resource.

## Problem outputs

The Statement editor presents `Text`, `PDF`, and `HTML` as peer output links.
Text renders the current language's `problem.tex` directly. PDF and HTML use
the transient Preview service: opening either link synchronously reuses a valid
content-identity cache entry or generates the missing result before returning
it. The editor does not expose a second compile/rebuild action or durable
ready/stale state.

Restoring default templates is an authored-source action, not a Preview action.
The editor offers it only when one of the three canonical Statement templates
differs from the repository default, is missing/invalid, or when the optional
editable `statement/examples.tex` override exists.

## Contest review

Contest Statement Review computes the common Statement language set for the
selected Workspace or Native Package source. Opening a Review link builds or
reuses every Problem HTML preview in canonical Contest `idx` order through a
bounded worker pool and blocks until every item finishes. The response is one
long document with no intermediate generation page, iframe, fragment polling,
or client-side progressive replacement. A failed Problem occupies its original
position with its diagnostic and complete available Pandoc log; other successful
statements remain visible.

An explicitly requested language is used as-is after canonical validation.
Without an explicit language, Preview chooses English only when it is actually
available, otherwise it chooses the first available language. No synthetic
English fallback is created when the Contest has no Statement language.

Contest Statement metadata resolves each localized property through
`<property>.<language>` and then its base key. Thus `title.chinese` overrides
`title` only for Chinese rendering, while an absent or empty override inherits
the base value. This applies to user-defined properties as well as the default
template's fields. Every effective key is injected directly into the FTL
context and is also present in its `properties` mapping. The editor persists
these values as one property mapping; it does not maintain one SQL column per
field or language.

Contest Statement resources have two source scopes. Files below
`statements/_shared/` are available to every Statement language; files below
`statements/<language>/` are specific to that language. Preview copies the
shared tree first and the selected language tree second, so a language-specific
file with the same relative path overrides the shared file. The entry templates
`statements.ftl` and `olymp.sty` remain language-specific and MUST NOT be stored
in the shared scope. The shared scope is additive and does not supply either
language-specific entry file.
The editor labels this scope `Shared`. Removing a language from the editor
deletes only that language's Contest source overrides; it does not remove the
language from the constituent Problems. Consequently, a language supported by
every Problem can remain available through the default Contest templates after
its saved overrides are removed.

Polygon Contest packages use `statements/<language>/statements.tex` for the
authored Contest entry source. Import normalizes that external entry name to
the canonical internal `statements.ftl` name while preserving its source
content; `statements.tex` remains reserved for Polygon Replica's derived
render output.

## Contest PDF

Contest PDF Preview is the existing complete Contest TeX build placed under the
Preview lifecycle. The authored `statements.ftl` is an FTL source. Preview
renders it with the effective Contest property mapping and the canonical
ordered Problem entries into the derived `statements.tex` before compiling it.
The default template emits the `olymp.sty` blank-page signal only when the
Boolean `insertBlankPage` value is true. A non-empty `banner` value replaces
the existing `\StatementBanner` slot and may reference a resource from the
Contest Statement Sources tree. Preview does not patch TeX text after FTL
rendering.
The complete document includes the Contest cover/header, Contest attachments,
every Problem render tree, MetaPost and bounding-box preparation, and two
XeLaTeX passes. The result is one PDF generated by TeX.
There is no per-Problem PDF generation and no PDF merge step. Opening a PDF
Preview link blocks while the cached or missing complete Contest PDF is
resolved, then returns the PDF inline. A failure returns the same plain-text
error excerpt and complete `latex.log` projection as Problem PDF Preview; there
is no separate Generate/Rebuild page.

The blank-page option is a persisted Contest Property. It is not submitted by
the Preview request, and therefore all Workspace and Native Package PDF links
for one Contest use the same setting.

The PDF, lifecycle diagnostics, and complete sanitized LaTeX log live below the
Preview cache root. Contest package downloads never create a Statement PDF or
durable Contest artifact.

Problem and Contest PDF failures use the same diagnostic projection. The
bounded error excerpt begins at the first LaTeX `!` error line and retains the
following context. The Preview cache also stores the complete sanitized
`latex.log`; the failure response displays that log after the excerpt. The
complete log is not copied into the SQLite Preview summary.

## Identity and invalidation

A Preview input identity describes only the subject, source, language, output
kind, source content/revision, Statement resources, sample evidence, and explicit
output options. It never contains Pandoc, Poppler, TeX, Lua filter, renderer,
Python package, executable, container, or toolchain version/hash.

All Statement Preview records and payloads are disposable cache. Startup and
renderer/toolchain deployment invalidate every successful or in-flight result
and clear the cache root. A missing preview payload is a cache miss, not source
or package corruption.

## Sandbox limits

Pandoc, the Lua filter, Poppler, librsvg and TeX run through bubblewrap with no
network, read-only source mounts, isolated writable output, and CPU, memory,
process, file-output and timeout limits. Image count, expanded image bytes,
Pandoc AST size, sanitized HTML size, and Statement sample bytes are bounded.

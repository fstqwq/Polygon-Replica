# Polygon World Finals-style statement templates

This directory contains the canonical statement templates and LaTeX style used
by Qiulygon. The style is not an official ICPC asset; it is derived from the
Polygon-compatible `olymp.sty` ecosystem.

## Hard-fork status

This directory is a project-owned **hard fork**, not a synchronized vendor
snapshot or a Git submodule. Its upstream lineage is
[`GassaFM/olymp.sty`](https://github.com/GassaFM/olymp.sty), followed by
[`fstqwq/Polygon-WF-Styles`](https://github.com/fstqwq/Polygon-WF-Styles), and
then the copy maintained inside Polygon Replica.

Polygon Replica deliberately preserves selected Polygon-facing commands while
independently evolving the FreeMarker split, XeLaTeX support, sample resource
pipeline, structured multipass and interaction presentation, blank-page
behavior, and banner contract. New interfaces and behavior documented here
must not be assumed to exist in either upstream repository. Conversely,
upstream changes must be reviewed and ported intentionally; replacing this
directory wholesale with an upstream checkout is unsupported.

## Template files

- `statements.ftl` is the outer document template.
- `problem.tex` is the per-problem FreeMarker template. It always inputs the
  rendered `examples.tex` companion.
- `examples.tex` is the default FreeMarker template for sample presentation.
- `olymp.sty` implements both Polygon sample commands and the structured sample
  presentation API.

In a problem source tree, `statement/examples.tex` is optional. When absent,
the renderer uses this directory's canonical `examples.tex`. When present, its
contents replace the default and are rendered with the same context as
`statement/problem.tex`. An empty authored file intentionally produces no
examples. A pre-existing custom `statement/problem.tex` is not rewritten and
must input `examples.tex` itself if it wants to use the companion.

## Polygon-compatible samples

The following interfaces retain their existing two-column behavior:

```tex
\begin{example}
  \exmp{input text}{output text}
  \exmpfile{sample.in}{sample.ans}
  \exmpinteract{interactor text}{solution text}
  \exmpinteractfile{interaction.in}{interaction.ans}
\end{example}
```

The default `examples.tex` uses `problem.sampleTests` through `\exmpfile` when
no structured sample context is available. Existing Polygon packages therefore
continue to render without adding or changing their sample schema.

The presence of `problem.examples.samples` is authoritative. An explicitly
empty structured sample array renders no examples rather than falling back to
`problem.sampleTests`.

## Structured samples

The structured API accepts sample, pass, and event order explicitly:

```tex
\begin{StatementSamples}
  \StatementSampleFile{1}{sample.in}{sample.ans}

  \StatementSamplePassFile{2}{1}{pass-1.in}{pass-1.ans}
  \StatementSamplePassFile{2}{2}{pass-2.in}{pass-2.ans}
  \StatementSamplePassFile{2}{5}{pass-5.in}{pass-5.ans}

  \begin{StatementSampleInteraction}[3]{3}
    \StatementSampleEventFile{interactor}{events/001.txt}
    \StatementSampleEventFile{solution}{events/002.txt}
    \StatementSampleEventFile{interactor}{events/003.txt}
  \end{StatementSampleInteraction}
\end{StatementSamples}
```

`\StatementSampleFile{sample}{inputFile}{outputFile}` renders one ordinary
sample. `\StatementSamplePassFile{sample}{pass}{inputFile}{outputFile}` renders
one explicitly numbered pass. Pass numbers need not be consecutive.

`StatementSampleInteraction` takes an optional pass number followed by the
sample number. Without the optional value its title is `Sample Interaction N`;
with it, the title is `Sample N, Pass P`.

Each `\StatementSampleEventFile{source}{textFile}` creates one event block.
`interactor` is placed under Read on the left and `solution` under Write on the
right. Consecutive events from the same source remain separate. Unknown source
values produce a LaTeX package error. There is no event `kind` or EOF record;
only visible events are supplied.

All file-backed sample commands share the same `fancyvrb` reader. Leading empty
lines, internal empty lines, and line endings are handled consistently for
ordinary samples, passes, and interaction events.

## FreeMarker structured context

The canonical `examples.tex` recognizes this optional render-context shape:

```json
{
  "problem": {
    "examples": {
      "samples": [
        {
          "number": 1,
          "presentation": "pair",
          "passes": [
            {
              "number": 1,
              "inputFile": "sample-1.in",
              "outputFile": "sample-1.ans"
            }
          ]
        },
        {
          "number": 2,
          "presentation": "interaction",
          "passes": [
            {
              "number": 3,
              "events": [
                {
                  "source": "interactor",
                  "textFile": "events/001.txt"
                },
                {
                  "source": "solution",
                  "textFile": "events/002.txt"
                }
              ]
            }
          ]
        }
      ]
    },
    "sampleTests": []
  }
}
```

Polygon Replica's current `StatementExamplesProducer` synthesizes
`problem.examples.samples` for browser previews and verified-revision statement
builds. It projects authored inline `sample_json`, explicit display overrides,
or canonical main-correct pass evidence into the structured context and writes
each referenced payload as a controlled UTF-8 render resource. The producer
also supplies `problem.sampleTests` as a compatibility projection for custom
templates that still consume the Polygon shape.

The generated `inputFile`, `outputFile`, and `textFile` paths are relative to
the rendered problem's compile directory and exist before FreeMarker output is
compiled by TeX. Another compatible rendering caller may provide the same
context and resources directly. Authors may also replace
`statement/examples.tex` with arbitrary FreeMarker and TeX.

## Blank pages and banners

The files under [`banner/`](banner/README.md) are mock title/banner assets. See
their README for the ICPC logo trademark notice and usage limits.

The default `statements.ftl` emits the existing `olymp.sty` signal when its
`insertBlankPage` context value is true:

```tex
\intentionallyblankpagestrue
\intentionallyblankpagesfalse
```

Custom templates may consume the same Boolean context or use the signal
directly. All effective Contest Properties are injected as top-level FTL
values and through the `properties` mapping. No post-render text replacement
is performed.

No wrapper commands are required. The `banner` Contest Property is empty by
default. A non-empty value makes the default template replace the existing
banner slot:

```tex
\renewcommand{\StatementBanner}{%
  \includegraphics[width=\textwidth]{statement-banner.pdf}%
}
```

## License

See the original `olymp.sty` project: <https://github.com/GassaFM/olymp.sty>.

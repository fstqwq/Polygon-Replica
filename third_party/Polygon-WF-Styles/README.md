# Polygon World Finals-style statement templates

This directory contains the canonical statement templates and LaTeX style used
by Qiulygon. The style is not an official ICPC asset; it is derived from the
Polygon-compatible `olymp.sty` ecosystem.

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

The current renderer continues to produce `problem.sampleTests`; it does not
synthesize `problem.examples`. The structured branch is the consumer contract
for a future producer or another compatible rendering caller. Authors may also
replace `statement/examples.tex` with arbitrary FreeMarker and TeX immediately.
Every referenced `inputFile`, `outputFile`, and `textFile` must be materialized
as a UTF-8 text file relative to the rendered problem's compile directory.

## Blank pages and banners

Use the existing `olymp.sty` signal directly:

```tex
\intentionallyblankpagestrue
\intentionallyblankpagesfalse
```

No wrapper commands are required. The banner command is empty by default and
can be replaced in a template:

```tex
\renewcommand{\StatementBanner}{%
  \includegraphics[width=\textwidth]{statement-banner.pdf}%
}
```

## License

See the original `olymp.sty` project: <https://github.com/GassaFM/olymp.sty>.

\documentclass [11pt, a4paper, oneside] {article}

% --- Engine-adaptive font loading ---
% This template is compiled with xelatex when fontspec is present (see below).
% The engine is auto-detected by latex_process.detect_latex_engine().
\usepackage {fontspec}
\usepackage {xeCJK}

% CJK fonts: serif body + sans headings (standard CJK typographic convention).
% ItalicFont=<self> prevents "font shape undefined" warnings (CJK has no italic).
\setCJKmainfont{Noto Serif CJK SC}[ItalicFont=Noto Serif CJK SC]
\setCJKsansfont{Noto Sans CJK SC}[ItalicFont=Noto Sans CJK SC]
\setCJKmonofont{Noto Sans CJK SC}

\usepackage {amsmath}
\usepackage {amssymb}
\usepackage {olymp}
\usepackage {comment}
\usepackage {epigraph}
\usepackage {expdlist}
\usepackage {graphicx}
\usepackage {multirow}
\usepackage {siunitx}
\usepackage [normalem] {ulem}
%\usepackage {hyperref}
\usepackage {import}
\usepackage {xparse}
\usepackage {wrapfig}
\usepackage {comment}

\intentionallyblankpagestrue

\begin {document}

\contest
{${contest.name!}}%
{${contest.location!}}%
{${contest.date!}}%

\binoppenalty=10000
\relpenalty=10000

\renewcommand{\t}{\texttt}
\renewcommand{\thefootnote}{\fnsymbol{footnote}}

<#if shortProblemTitle?? && shortProblemTitle>
  \def\ShortProblemTitle{}
</#if>

<#list statements as statement>
<#if statement.path??>
\graphicspath{{${statement.path}}}
<#if statement.index??>
  \def\ProblemIndex{${statement.index}}
</#if>
\import{${statement.path}}{./${statement.file}}
<#else>
\input ${statement.file}
</#if>
</#list>

\end {document}

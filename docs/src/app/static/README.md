# `app/static`

Static browser assets are served by the web application. They contain client
JavaScript, styles, icons, and other immutable presentation resources. They do
not own authorization or domain state; endpoints validate every state-changing
request independently of client behavior.

Core delegates popup opening to document clicks and maintains one active modal.
Opening the same overlay retains its focus trap and inert state. Opening another
closes the previous modal and restores its background state before applying the
new modal's state. `polygonlike:popup-opened` includes the overlay and opener;
`polygonlike:popup-closed` accompanies the common close path. Delayed focus checks
the active modal identity.

Run details retain one pending request keyed by URL, verification, testcase, and
program. Repeated clicks on its key share that request. Switching or closing aborts
it; completion, failure, and cleanup check request identity before changing UI.
Reopening after completion fetches current diagnostics. Closing clears content.
The opener supplies the program identity; its testcase row supplies the shared
test name, source kind, and generator command.
The executor Node harness exercises the real modules with controlled events and
fetch; browser layout and paint require a separate browser check.

The profile reads Navigation Timing and Server-Timing. TTFB is response start minus
request start; estimated network overhead is TTFB minus application elapsed time. Unavailable metrics
display `n/a`.

The admin judgehost command generator starts with the request's current site
URL and retains explicit operator overrides for private proxy endpoints. Clearing
the URL restores that site default.

## Contest marks

`contest_marks.js` owns one inline editor on the Contest Problems page. Enter or
focus leaving the editor saves through the property endpoint; Escape cancels.
Emoji choices preserve trailing text. Pending writes are serialized, and failed
writes preserve the draft for retry. Displayed values use plain text.

Contest marks share the limits row and align with the problem name using the
owner-prefix grid width. Empty marks use a tag icon; the inline editor overlays
the available mark width while its hidden trigger preserves table geometry.

## Verification timing preview

The timing preview uses the shared popup and its focus, inert, Escape and close
lifecycle. The metric selector chooses CPU or wall time. Twenty program rows fit
common desktop heights; smaller windows and longer program lists use pagination.
The time axis also carries the safety bands and a draggable 500 ms limit. All
programs influence the bands and recommendation, including those on other pages.

The browser projects completed results using persisted answer correctness and
required/allowed verdict sets. CPU projection preserves censored TL results.
Wall projection treats a censored result as TL below its observed elapsed time
and unknown at or above it. Missing measurements remain unknown. Compilation,
runtime and infrastructure errors retain their verdicts. Complete correct runs
use the existing inclusive 0.5-1.5 times limit warning band; censored runtimes do
not establish warning margins. Red means a definite Expected mismatch, gray
means incomplete evidence, yellow means a runtime warning, and green means
complete matching results without warnings, in that precedence order. The first
green 500 ms choice is suggested when an Expected AC solution is present. This
is a preview of recorded evidence; it does not modify limits or promise the
outcome of a rerun with higher execution limits.

Timing modules use versioned static URLs. Data is decoded once, sorted indexes
and safety intervals are prepared per metric, and plots are built on popup open
or layout changes. A drag updates only the line and result labels when it crosses
a selectable limit. The original result matrix is unchanged.

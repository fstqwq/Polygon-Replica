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
lifecycle. The metric selector chooses CPU or wall time. All programs appear in
one list, grouped by background (AC, TL, WA, other), then lexically by program
name and source path. Longer lists use the shared popup's scrolling content.
The time axis also carries the safety bands and a draggable 500 ms limit. All
programs influence the bands and slack values.
Hovering or focusing a point highlights the same testcase across all programs.
The hovered program, testcase and runtime appear beside the popup title. A positive
runtime multiplier scales observed metrics, warning boundaries, censored wall-time
bounds and slack while keeping the selected limit and recorded data unchanged.
Highlight updates are coalesced per animation frame and touch only the previous
and next testcase groups. Point hit areas stay fixed. The cutoff overlay keeps
points interactive while shading the portion beyond the selected limit.
Safety bands use exact runtime and warning boundaries, including below 500 ms.
The selected limit uses the 500 ms grid and evaluates inclusive
boundary rules directly. Nonzero task counts appear alongside verification status.
After completion, the Timing analysis entry appears between status and task counts.

The browser projects completed results using persisted answer correctness and
required/allowed verdict sets. CPU projection preserves censored TL results.
Wall projection treats a censored result as TL below its observed elapsed time
and unknown at or above it. Missing measurements remain unknown. Compilation,
runtime and infrastructure errors retain their verdicts. Complete correct runs
use the existing inclusive 0.5-1.5 times limit warning band; censored runtimes do
not establish warning margins. Red means a definite Expected mismatch, gray
means incomplete evidence, yellow means a runtime warning, and green means
complete matching results without warnings, in that precedence order. Above the
selected limit, plus/minus labels show the margins before an unexpected result:
plus is room to lower the limit, minus is room to raise it.
Warnings remain inside the usable range. Required verdicts apply per program,
so a required TL disappears only after its last timed-out testcase completes.
Censored measurements bound known runtime; they do not prove an unlimited
margin, including when the CPU verdict projection retains TL. Only finite
margins are displayed; unknown or unbounded margins are hidden.
A current unexpected result hides the margins.
Beyond a censored runtime, the upper margin remains unknown;
the lower margin retains the known failure boundary below the preceding
matching interval. A definite mismatch blocks that continuation.
The plotted axis endpoint does not impose a bound.
This is a preview of recorded evidence; it does not modify limits or promise the
outcome of a rerun with higher execution limits.

Timing modules use versioned static URLs. Data is decoded once, sorted indexes
and safety intervals are prepared per metric, and plots are built on popup open
or layout changes. A drag updates only the line and result labels when it crosses
a selectable limit. The original result matrix is unchanged.

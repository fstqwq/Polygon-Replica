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
The executor Node harness exercises the real modules with controlled events and
fetch; browser layout and paint require a separate browser check.

The profile reads Navigation Timing and Server-Timing. TTFB is response start minus
request start; transfer is response end minus response start. Unavailable metrics
display `n/a`.

# `app/impl/run_export`

Coordinates authorized verification pages, testcase fragments, sample downloads,
and package export actions. Workspace and Contest resolvers provide the actual
scope and navigation; access queries enforce read permissions before evidence
materialization.

Testcase fragments and sample JSON use persisted verification mode, source and
pass evidence through the scoped testcase read model. Shared display helpers project
cells, diagnostics and artifact previews for both complete pages and fragments.
Malformed persisted mode produces unavailable sample evidence. Fragment reads do
not refresh Git status or normalize authored builds. Complete workspace pages use
the authoring context, including its status refresh and build normalization.

The verification list retains its Created timestamp and displays elapsed time
below it. Active verifications use the server's current time at page render;
terminal verifications use their persisted finish time. Duration includes time
since creation and is shown in seconds or minutes and seconds.

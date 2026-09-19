# `app/template`

Templates render server-side HTML for the web workflow. Context construction
and access decisions belong to `app/impl` and services. Templates may present
status and links but are not authorities for lifecycle, derived-data availability,
or permissions.

The shared template renderer records elapsed and thread CPU milliseconds around
template generation for pages and fragments. At response start, the ASGI middleware
reports `application`, `template`, and `template_cpu` in `Server-Timing`.
Application elapsed time includes authentication and rendering; the compatible
`X-Backend-Render-Ms` header reports that elapsed time. The profile presents these
server measurements alongside browser TTFB and response transfer time.

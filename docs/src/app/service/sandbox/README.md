# `app/service/sandbox`

Owns the bubblewrap-backed local process boundary used by TeX compilation and supporting operations. Inputs define command, working directory, mounts, environment, stdio, and resource limits; outputs contain status, return code, usage observations, and bounded output.

The backend validates mounts, creates a per-call isolated process, and retains
no durable state. The per-call process limit is applied inside the bubblewrap
user namespace; host processes that happen to share the runtime account's
numeric UID are outside that budget. Linux bubblewrap/user-namespace capability
is probed by the host installer. Failures return to the calling domain; the
sandbox owns neither verification nor preview lifecycle.

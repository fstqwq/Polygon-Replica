# `app/service/sandbox`

Owns local process isolation and resource-controlled execution used for preview
and supporting operations. Linux bubblewrap/user-namespace capability is probed
by the host installer. Sandbox failures are reported to the calling domain; the
sandbox does not own verification lifecycle.

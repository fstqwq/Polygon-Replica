# `app/service/sandbox`

Owns the local `ExecSpec`/`ExecResult` boundary and the bubblewrap-backed process
runner used by TeX compilation and supporting local operations. Inputs include
command, working directory, mounts, environment, stdio paths, timeout, memory,
process, and output limits. The result contains backend/status, return code,
elapsed time, resource observations, and bounded output.

The backend validates mounts, creates a per-call isolated process, and retains
no durable state. Linux bubblewrap/user-namespace capability is probed by the
host installer. Failures return to the calling domain; the sandbox owns neither
verification nor preview lifecycle.

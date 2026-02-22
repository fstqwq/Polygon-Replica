#!/usr/bin/env bash
set -euo pipefail

export POLYGONLIKE_DB=${POLYGONLIKE_DB:-./var/polygonlike.db}
export POLYGONLIKE_BARE_ROOT=${POLYGONLIKE_BARE_ROOT:-./var/srv/git}
export POLYGONLIKE_WORKSPACE_ROOT=${POLYGONLIKE_WORKSPACE_ROOT:-./var/srv/workspaces}
export POLYGONLIKE_RUN_ROOT=${POLYGONLIKE_RUN_ROOT:-./var/srv/runs}
export POLYGONLIKE_ARTIFACTS_ROOT=${POLYGONLIKE_ARTIFACTS_ROOT:-./var/lib/polygonlike/artifacts}
export POLYGONLIKE_CACHE_ROOT=${POLYGONLIKE_CACHE_ROOT:-./var/cache/polygonlike}

mkdir -p ./var
uvicorn app.main:app --reload

#!/usr/bin/env bash
set -euo pipefail

: "${POLYGONLIKE_BARE_ROOT:=/srv/git}"
: "${POLYGONLIKE_WORKSPACE_ROOT:=/srv/workspaces}"
: "${POLYGONLIKE_RUN_ROOT:=/srv/runs}"
: "${POLYGONLIKE_ARTIFACTS_ROOT:=/var/lib/polygonlike/artifacts}"
: "${POLYGONLIKE_CACHE_ROOT:=/var/cache/polygonlike}"

mkdir -p "$POLYGONLIKE_BARE_ROOT" "$POLYGONLIKE_WORKSPACE_ROOT" "$POLYGONLIKE_RUN_ROOT" "$POLYGONLIKE_ARTIFACTS_ROOT" "$POLYGONLIKE_CACHE_ROOT"
echo "initialized host directories"

#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 <existing-env-file> <output-file>" >&2
  exit 2
fi

EXISTING_ENV_FILE="$1"
OUTPUT_FILE="$2"

MANAGED_KEYS=(
  POLYGON_REPLICA_DB
  POLYGON_REPLICA_BARE_ROOT
  POLYGON_REPLICA_WORKSPACE_ROOT
  POLYGON_REPLICA_ARTIFACTS_ROOT
  POLYGON_REPLICA_CACHE_ROOT
  POLYGON_REPLICA_CONTEST_SOURCE_ROOT
  POLYGON_REPLICA_BACKUP_ROOT
  POLYGON_REPLICA_TLS_KEY_PATH
  POLYGON_REPLICA_TLS_CERT_PATH
)

declare -A MANAGED_VALUES=(
  [POLYGON_REPLICA_DB]="/var/lib/polygon-replica/metadata.db"
  [POLYGON_REPLICA_BARE_ROOT]="/srv/polygon-replica/git"
  [POLYGON_REPLICA_WORKSPACE_ROOT]="/srv/polygon-replica/workspaces"
  [POLYGON_REPLICA_ARTIFACTS_ROOT]="/srv/polygon-replica/export"
  [POLYGON_REPLICA_CACHE_ROOT]="/tmp/polygon-replica"
  [POLYGON_REPLICA_CONTEST_SOURCE_ROOT]="/var/lib/polygon-replica/contest-sources"
  [POLYGON_REPLICA_BACKUP_ROOT]="/var/backups/polygon-replica"
  [POLYGON_REPLICA_TLS_KEY_PATH]="/var/lib/polygon-replica/tls/dev-localhost.key"
  [POLYGON_REPLICA_TLS_CERT_PATH]="/var/lib/polygon-replica/tls/dev-localhost.crt"
)

declare -A SEEN_KEYS=()
PRESERVED_LINES=()

systemd_value_is_single_line() {
  local value="$1"
  local trimmed="${value#"${value%%[![:space:]]*}"}"
  local state="unquoted"
  local escaped=0
  local char
  local index
  local start_index=0
  if [[ "${trimmed:0:1}" == "'" ]]; then
    state="single"
    start_index=1
  elif [[ "${trimmed:0:1}" == '"' ]]; then
    state="double"
    start_index=1
  fi
  for ((index = start_index; index < ${#trimmed}; index++)); do
    char="${trimmed:index:1}"
    if [[ "$state" == "single" ]]; then
      if [[ "$char" == "'" ]]; then
        state="closed"
      fi
      continue
    fi
    if [[ "$state" == "closed" ]]; then
      if [[ ! "$char" =~ [[:space:]] ]]; then
        return 1
      fi
      continue
    fi
    if [[ "$escaped" -eq 1 ]]; then
      escaped=0
      continue
    fi
    if [[ "$char" == "\\" ]]; then
      escaped=1
    elif [[ "$state" == "double" ]]; then
      if [[ "$char" == '"' ]]; then
        state="closed"
      fi
    fi
  done
  [[ ( "$state" == "unquoted" || "$state" == "closed" ) && "$escaped" -eq 0 ]]
}

LINE_NUMBER=0
while IFS= read -r line || [[ -n "$line" ]]; do
  LINE_NUMBER=$((LINE_NUMBER + 1))
  trimmed="${line#"${line%%[![:space:]]*}"}"
  if [[ -z "$trimmed" || "$trimmed" == \#* || "$trimmed" == \;* ]]; then
    PRESERVED_LINES+=("$line")
    continue
  fi
  if [[ ! "$trimmed" =~ ^(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
    echo "invalid environment record at line $LINE_NUMBER" >&2
    exit 1
  fi
  key="${BASH_REMATCH[2]}"
  value="${BASH_REMATCH[3]}"
  if ! systemd_value_is_single_line "$value"; then
    echo "invalid environment value at line $LINE_NUMBER: $key" >&2
    exit 1
  fi
  if [[ -n "${SEEN_KEYS[$key]+present}" ]]; then
    echo "duplicate environment key at line $LINE_NUMBER: $key" >&2
    exit 1
  fi
  SEEN_KEYS["$key"]=1
  if [[ -n "${MANAGED_VALUES[$key]+managed}" ]]; then
    continue
  fi
  PRESERVED_LINES+=("$key=$value")
done <"$EXISTING_ENV_FILE"

: >"$OUTPUT_FILE"
for key in "${MANAGED_KEYS[@]}"; do
  printf '%s=%s\n' "$key" "${MANAGED_VALUES[$key]}" >>"$OUTPUT_FILE"
done
for line in "${PRESERVED_LINES[@]}"; do
  printf '%s\n' "$line" >>"$OUTPUT_FILE"
done

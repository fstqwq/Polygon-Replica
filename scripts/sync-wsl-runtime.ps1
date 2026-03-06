[CmdletBinding()]
param(
    [string]$Distro = "PolygonReplica-Dev",
    [string]$WslRepoPath = "/root/work/Polygon-Replica",
    [string]$ServiceName = "polygon-replica.service",
    [switch]$NoRestart,
    [switch]$NoHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-WslBash {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Script
    )
    $normalized = $Script -replace "`r", ""
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $encoded = [System.Convert]::ToBase64String($bytes)
    $launcher = "printf '%s' '$encoded' | base64 -d | bash"
    & wsl -d $Distro -- bash -lc $launcher
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed (exit=$LASTEXITCODE)"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRootEscaped = $repoRoot.Replace("\", "\\")
$wslRepoPathEscaped = $WslRepoPath.Replace("'", "'\''")
$serviceNameEscaped = $ServiceName.Replace("'", "'\''")

Write-Host "[sync] source (Windows): $repoRoot"
Write-Host "[sync] target (WSL): $WslRepoPath"
Write-Host "[sync] distro: $Distro"

$syncScript = @"
set -euo pipefail
src=`$(wslpath -a '$repoRootEscaped')
dst='$wslRepoPathEscaped'

if [ ! -d "`$src/.git" ]; then
  echo "source is not a git repository: `$src" >&2
  exit 2
fi

mkdir -p "`$dst"
tar --exclude=.git --exclude=.venv --exclude=var -cf - -C "`$src" . | tar -xf - -C "`$dst"
echo "sync done"
"@
Invoke-WslBash -Script $syncScript

if (-not $NoRestart) {
    $restartScript = @"
set -euo pipefail
systemctl restart '$serviceNameEscaped'
systemctl is-active '$serviceNameEscaped'
"@
    Write-Host "[restart] restarting $ServiceName"
    Invoke-WslBash -Script $restartScript
}

if (-not $NoHealthCheck) {
    $healthScript = @"
set -euo pipefail
if command -v curl >/dev/null 2>&1; then
  code="000"
  for ((i=0; i<40; i++)); do
    code=`$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/ || true)
    if [ "`$code" != "000" ]; then
      break
    fi
    sleep 0.25
  done
  echo "http://127.0.0.1:8001 -> `$code"
  if [ "`$code" = "000" ]; then
    echo "health check failed: service not reachable on 8001" >&2
    exit 3
  fi
fi
ss -ltnp | grep ':8001' || true
"@
    Write-Host "[health] checking 8001"
    Invoke-WslBash -Script $healthScript
}

Write-Host "[done] WSL runtime sync completed."

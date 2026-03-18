#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer only supports Linux hosts." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Debian/Ubuntu (apt-get)." >&2
  echo "Install dependencies manually, then rerun with a compatible package manager script." >&2
  exit 1
fi

SUDO=()
if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
  else
    echo "Please run as root or install sudo." >&2
    exit 1
  fi
fi

RUNTIME_USER="${SUDO_USER:-$(id -un)}"
RUNTIME_GROUP="$(id -gn "$RUNTIME_USER")"

echo "[1/5] Installing system dependencies..."
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
  git \
  python3 \
  python3-venv \
  python3-pip \
  texlive-latex-base \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-science \
  texlive-lang-cyrillic \
  texlive-fonts-recommended \
  cm-super \
  util-linux \
  bubblewrap \
  libseccomp2

echo "  Initializing TeX formats..."
if command -v fmtutil-sys >/dev/null 2>&1; then
  "${SUDO[@]}" mktexlsr >/dev/null 2>&1 || true
  "${SUDO[@]}" fmtutil-sys --byfmt pdflatex >/dev/null
fi

echo "[2/5] Configuring kernel user namespace settings..."
SYSCTL_FILE="/etc/sysctl.d/99-polygon-replica-sandbox.conf"
TMP_SYSCTL="$(mktemp)"
cat >"$TMP_SYSCTL" <<'EOF'
kernel.unprivileged_userns_clone = 1
user.max_user_namespaces = 1048576
EOF
if [[ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
  cat >>"$TMP_SYSCTL" <<'EOF'
kernel.apparmor_restrict_unprivileged_userns = 0
EOF
fi
"${SUDO[@]}" install -m 0644 "$TMP_SYSCTL" "$SYSCTL_FILE"
rm -f "$TMP_SYSCTL"
"${SUDO[@]}" sysctl --system >/dev/null

USERNS_CLONE="$(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || echo "unknown")"
USERNS_MAX="$(sysctl -n user.max_user_namespaces 2>/dev/null || echo "unknown")"
APPARMOR_USERNS="$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo "n/a")"
echo "  kernel.unprivileged_userns_clone=$USERNS_CLONE"
echo "  user.max_user_namespaces=$USERNS_MAX"
echo "  kernel.apparmor_restrict_unprivileged_userns=$APPARMOR_USERNS"

echo "  Preparing runtime storage roots..."
RUNTIME_DIRS=(
  /srv/git
  /srv/workspaces
  /srv/runs
  /var/lib/polygon-replica
  /var/lib/polygon-replica/artifacts
  /var/lib/polygon-replica/tls
  /var/cache/polygon-replica
)
"${SUDO[@]}" install -d -m 0755 "${RUNTIME_DIRS[@]}"
"${SUDO[@]}" chown -R "${RUNTIME_USER}:${RUNTIME_GROUP}" \
  /srv/git \
  /srv/workspaces \
  /srv/runs \
  /var/lib/polygon-replica \
  /var/cache/polygon-replica

echo "[3/5] Probing bubblewrap root-switch capability..."
PROBE_CMD=(bwrap --die-with-parent --new-session --ro-bind / / --chdir / -- /bin/sh -lc 'true')
if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  if ! sudo -u "$RUNTIME_USER" "${PROBE_CMD[@]}" >/dev/null 2>&1; then
    echo "bubblewrap probe failed for runtime user: $RUNTIME_USER." >&2
    echo "Your environment still blocks user namespace / uid_map operations." >&2
    echo "Sandbox root-switch cannot be enabled safely yet." >&2
    echo "Check container security policy, seccomp, or host kernel settings." >&2
    exit 1
  fi
else
  if ! "${PROBE_CMD[@]}" >/dev/null 2>&1; then
    echo "bubblewrap probe failed." >&2
    echo "Your environment still blocks user namespace / uid_map operations." >&2
    echo "Sandbox root-switch cannot be enabled safely yet." >&2
    echo "Check container security policy, seccomp, or host kernel settings." >&2
    exit 1
  fi
fi
echo "  bubblewrap probe passed."

echo "  Probing TeX runtime (pdflatex format)..."
if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  if ! sudo -u "$RUNTIME_USER" /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\begin{document}
ok
\end{document}
EOF
pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "pdflatex runtime probe failed for user: $RUNTIME_USER." >&2
    echo "Try: fmtutil -user --byfmt pdflatex" >&2
    exit 1
  fi
else
  if ! /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\begin{document}
ok
\end{document}
EOF
pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "pdflatex runtime probe failed." >&2
    echo "Try: fmtutil -user --byfmt pdflatex" >&2
    exit 1
  fi
fi
echo "  pdflatex probe passed."

echo "  Probing TeX T2A encoding support (t2aenc.def)..."
if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  if ! sudo -u "$RUNTIME_USER" /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\usepackage[T2A]{fontenc}
\begin{document}
ok
\end{document}
EOF
pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "TeX T2A probe failed for user: $RUNTIME_USER." >&2
    echo "Install: texlive-lang-cyrillic" >&2
    exit 1
  fi
else
  if ! /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\usepackage[T2A]{fontenc}
\begin{document}
ok
\end{document}
EOF
pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "TeX T2A probe failed." >&2
    echo "Install: texlive-lang-cyrillic" >&2
    exit 1
  fi
fi
echo "  TeX T2A probe passed."

echo "  Probing TeX T2A vector font support (cm-super)..."
if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  if ! sudo -u "$RUNTIME_USER" /bin/sh -lc 'kpsewhich sfrm1095.pfb >/dev/null 2>&1'; then
    echo "TeX vector font probe failed for user: $RUNTIME_USER." >&2
    echo "Install: cm-super (and run updmap-sys)" >&2
    exit 1
  fi
else
  if ! /bin/sh -lc 'kpsewhich sfrm1095.pfb >/dev/null 2>&1'; then
    echo "TeX vector font probe failed." >&2
    echo "Install: cm-super (and run updmap-sys)" >&2
    exit 1
  fi
fi
echo "  TeX vector font probe passed."

echo "[4/5] Creating Python virtualenv and installing requirements..."
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[5/5] Writing runtime environment file..."
TMP_ENV_FILE="$(mktemp)"
cat >"$TMP_ENV_FILE" <<'EOF'
export POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db
export POLYGON_REPLICA_BARE_ROOT=/srv/git
export POLYGON_REPLICA_WORKSPACE_ROOT=/srv/workspaces
export POLYGON_REPLICA_RUN_ROOT=/srv/runs
export POLYGON_REPLICA_ARTIFACTS_ROOT=/var/lib/polygon-replica/artifacts
export POLYGON_REPLICA_CACHE_ROOT=/var/cache/polygon-replica
export POLYGON_REPLICA_TLS_KEY_PATH=/var/lib/polygon-replica/tls/dev-localhost.key
export POLYGON_REPLICA_TLS_CERT_PATH=/var/lib/polygon-replica/tls/dev-localhost.crt
EOF
ENV_FILE="/etc/polygon-replica.env"
"${SUDO[@]}" install -m 0644 "$TMP_ENV_FILE" "$ENV_FILE"
rm -f "$TMP_ENV_FILE"

echo
echo "Install completed."
echo "Next steps:"
echo "  source .venv/bin/activate"
echo "  source /etc/polygon-replica.env"
echo "  ./scripts/start_local.sh"

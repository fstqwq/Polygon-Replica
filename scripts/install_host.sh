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

PYTHON_BIN="python3.14"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.14 is required. Install CPython 3.14 before running this installer." >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14))'; then
  echo "The python3.14 command does not provide CPython 3.14." >&2
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

if [[ -n "${POLYGON_REPLICA_RUNTIME_USER:-}" ]]; then
  RUNTIME_USER="$POLYGON_REPLICA_RUNTIME_USER"
elif [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  RUNTIME_USER="$SUDO_USER"
else
  RUNTIME_USER="$(id -un)"
fi
if ! id "$RUNTIME_USER" >/dev/null 2>&1; then
  echo "Runtime user does not exist: $RUNTIME_USER" >&2
  exit 1
fi
RUNTIME_GROUP="$(id -gn "$RUNTIME_USER")"
if [[ "${EUID}" -ne 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  echo "A non-root installer may only select its own account as runtime user." >&2
  exit 1
fi
if [[ "$(id -u "$RUNTIME_USER")" -eq 0 ]]; then
  echo "Refusing to run Polygon-Replica as root." >&2
  echo "Run this installer through sudo from the runtime account, or set" >&2
  echo "POLYGON_REPLICA_RUNTIME_USER to an existing non-root account." >&2
  exit 1
fi

echo "[1/6] Installing system dependencies..."
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
  git \
  texlive-latex-base \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-xetex \
  texlive-science \
  texlive-lang-chinese \
  texlive-lang-cyrillic \
  texlive-fonts-recommended \
  fonts-texgyre \
  fonts-noto-cjk \
  cm-super \
  pandoc \
  poppler-utils \
  librsvg2-bin \
  util-linux \
  bubblewrap \
  libseccomp2

echo "  Initializing TeX formats..."
if command -v fmtutil-sys >/dev/null 2>&1; then
  "${SUDO[@]}" mktexlsr >/dev/null
  "${SUDO[@]}" fmtutil-sys --byfmt pdflatex >/dev/null
  "${SUDO[@]}" fmtutil-sys --byfmt xelatex >/dev/null
fi

echo "[2/6] Configuring kernel user namespace settings..."
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
  /srv/polygon-replica
  /srv/polygon-replica/git
  /srv/polygon-replica/workspaces
  /srv/polygon-replica/export
  /var/lib/polygon-replica
  /var/lib/polygon-replica/tls
  /var/lib/polygon-replica/contest-sources
  /tmp/polygon-replica
  /var/backups/polygon-replica
)
"${SUDO[@]}" install -d -m 0755 "${RUNTIME_DIRS[@]}"
"${SUDO[@]}" chmod 0700 \
  /var/lib/polygon-replica \
  /var/lib/polygon-replica/tls \
  /var/lib/polygon-replica/contest-sources \
  /var/backups/polygon-replica
"${SUDO[@]}" chown -R "${RUNTIME_USER}:${RUNTIME_GROUP}" \
  /srv/polygon-replica \
  /var/lib/polygon-replica \
  /tmp/polygon-replica
"${SUDO[@]}" chown -R "${RUNTIME_USER}:${RUNTIME_GROUP}" \
  /var/backups/polygon-replica

echo "[3/6] Probing bubblewrap root-switch capability..."
PROBE_CMD=(bwrap --die-with-parent --new-session --ro-bind / / --chdir / -- /bin/sh -lc 'true')
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  if ! runuser -u "$RUNTIME_USER" -- "${PROBE_CMD[@]}" >/dev/null 2>&1; then
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
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  if ! runuser -u "$RUNTIME_USER" -- /bin/sh -lc '
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

echo "  Probing XeLaTeX runtime (fontspec + xeCJK)..."
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  if ! runuser -u "$RUNTIME_USER" -- /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\usepackage{fontspec}
\usepackage{xeCJK}
\setmainfont{TeX Gyre Termes}
\setCJKmainfont{Noto Serif CJK SC}
\begin{document}
XeLaTeX probe \char"4F60\char"597D
\end{document}
EOF
xelatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "xelatex runtime probe failed for user: $RUNTIME_USER." >&2
    echo "Install: texlive-xetex texlive-lang-chinese fonts-texgyre fonts-noto-cjk" >&2
    exit 1
  fi
else
  if ! /bin/sh -lc '
tmpd="$(mktemp -d)"
cat >"$tmpd/main.tex" <<EOF
\documentclass{article}
\usepackage{fontspec}
\usepackage{xeCJK}
\setmainfont{TeX Gyre Termes}
\setCJKmainfont{Noto Serif CJK SC}
\begin{document}
XeLaTeX probe \char"4F60\char"597D
\end{document}
EOF
xelatex -interaction=nonstopmode -halt-on-error -output-directory "$tmpd" "$tmpd/main.tex" >/dev/null 2>&1
'; then
    echo "xelatex runtime probe failed." >&2
    echo "Install: texlive-xetex texlive-lang-chinese fonts-texgyre fonts-noto-cjk" >&2
    exit 1
  fi
fi
echo "  xelatex probe passed."

echo "  Probing TeX T2A encoding support (t2aenc.def)..."
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  if ! runuser -u "$RUNTIME_USER" -- /bin/sh -lc '
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
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  if ! runuser -u "$RUNTIME_USER" -- /bin/sh -lc 'kpsewhich sfrm1095.pfb >/dev/null 2>&1'; then
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

echo "[4/6] Creating Python virtualenv and installing requirements..."
if [[ "${EUID}" -eq 0 && "$RUNTIME_USER" != "$(id -un)" ]]; then
  runuser -u "$RUNTIME_USER" -- "$PYTHON_BIN" -m venv --clear "$REPO_ROOT/.venv"
  runuser -u "$RUNTIME_USER" -- "$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
  runuser -u "$RUNTIME_USER" -- "$REPO_ROOT/.venv/bin/python" -m pip install -r "$REPO_ROOT/requirements.txt"
else
  "$PYTHON_BIN" -m venv --clear "$REPO_ROOT/.venv"
  "$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$REPO_ROOT/.venv/bin/python" -m pip install -r "$REPO_ROOT/requirements.txt"
fi

echo "[5/6] Writing runtime environment file..."
ENV_FILE="/etc/polygon-replica.env"
TMP_EXISTING_ENV_FILE="$(mktemp)"
TMP_RENDERED_ENV_FILE="$(mktemp)"
TMP_INSTALLED_ENV_FILE=""
cleanup_runtime_env_files() {
  rm -f "$TMP_EXISTING_ENV_FILE" "$TMP_RENDERED_ENV_FILE"
  if [[ -n "$TMP_INSTALLED_ENV_FILE" ]]; then
    "${SUDO[@]}" rm -f "$TMP_INSTALLED_ENV_FILE"
  fi
}
trap cleanup_runtime_env_files EXIT
if "${SUDO[@]}" test -f "$ENV_FILE"; then
  "${SUDO[@]}" cat "$ENV_FILE" >"$TMP_EXISTING_ENV_FILE"
fi
bash "$REPO_ROOT/scripts/render_runtime_env.sh" \
  "$TMP_EXISTING_ENV_FILE" \
  "$TMP_RENDERED_ENV_FILE"
TMP_INSTALLED_ENV_FILE="$("${SUDO[@]}" mktemp /etc/.polygon-replica.env.XXXXXX)"
"${SUDO[@]}" install -o root -g root -m 0600 \
  "$TMP_RENDERED_ENV_FILE" \
  "$TMP_INSTALLED_ENV_FILE"
"${SUDO[@]}" mv -f "$TMP_INSTALLED_ENV_FILE" "$ENV_FILE"
TMP_INSTALLED_ENV_FILE=""
cleanup_runtime_env_files
trap - EXIT

echo "[6/6] Installing systemd service..."
SERVICE_UNIT_SRC="$REPO_ROOT/scripts/systemd/polygon-replica.service"
SERVICE_UNIT_DST="/etc/systemd/system/polygon-replica.service"
TMP_SERVICE_UNIT="$(mktemp --suffix=.service)"
trap 'rm -f "$TMP_SERVICE_UNIT"' EXIT
bash "$REPO_ROOT/scripts/render_systemd_unit.sh" \
  "$SERVICE_UNIT_SRC" \
  "$TMP_SERVICE_UNIT" \
  "$RUNTIME_USER" \
  "$RUNTIME_GROUP" \
  "$REPO_ROOT"
if ! command -v systemd-analyze >/dev/null 2>&1; then
  echo "systemd-analyze is required to validate the generated service unit." >&2
  exit 1
fi
systemd-analyze verify "$TMP_SERVICE_UNIT"
"${SUDO[@]}" install -m 0644 "$TMP_SERVICE_UNIT" "$SERVICE_UNIT_DST"
rm -f "$TMP_SERVICE_UNIT"
trap - EXIT
"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now polygon-replica.service >/dev/null
"${SUDO[@]}" systemctl is-active polygon-replica.service

echo
echo "Install completed."
echo "Next steps:"
echo "  Restart with: sudo systemctl restart polygon-replica.service"
echo "  App health: http://127.0.0.1:8001/login"

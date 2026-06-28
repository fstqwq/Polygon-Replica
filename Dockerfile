# Polygon-Replica web service container.
#
# Mirrors scripts/install_host.sh: same apt set, same TeX format build, same
# /opt/polygon-replica layout, same judgehost runtime user. Bubblewrap relies
# on host kernel user-namespace settings; see docs/docker.md.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/polygon-replica/.venv \
    PATH=/opt/polygon-replica/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        openssl \
        python3 \
        python3-venv \
        python3-pip \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-plain-generic \
        texlive-xetex \
        texlive-science \
        texlive-lang-chinese \
        texlive-lang-cyrillic \
        texlive-fonts-recommended \
        fonts-noto-cjk \
        cm-super \
        util-linux \
        bubblewrap \
        libseccomp2 \
        tini \
    && mktexlsr >/dev/null 2>&1 || true \
    && fmtutil-sys --byfmt pdflatex >/dev/null \
    && fmtutil-sys --byfmt xelatex >/dev/null \
    && updmap-sys >/dev/null 2>&1 || true \
    && rm -rf /var/lib/apt/lists/*

# Replace the default ubuntu uid 1000 with our judgehost user so file
# ownership on bind mounts and named volumes lines up with the host install.
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd --gid 1000 judgehost \
    && useradd --uid 1000 --gid judgehost --create-home --shell /bin/bash judgehost

# Pre-create runtime roots inside the image so first-mount of an empty named
# volume inherits judgehost ownership.
RUN install -d -o judgehost -g judgehost -m 0755 \
        /opt/polygon-replica \
        /srv/polygon-replica \
        /srv/polygon-replica/git \
        /srv/polygon-replica/workspaces \
        /srv/polygon-replica/export \
        /var/lib/polygon-replica \
        /var/lib/polygon-replica/tls \
        /tmp/polygon-replica

WORKDIR /opt/polygon-replica
USER judgehost

COPY --chown=judgehost:judgehost requirements.txt ./requirements.txt
RUN python3 -m venv .venv \
    && .venv/bin/pip install --upgrade pip \
    && .venv/bin/pip install -r requirements.txt

COPY --chown=judgehost:judgehost app ./app
COPY --chown=judgehost:judgehost scripts ./scripts
# third_party/upstream supplies testlib + standard checkers; Polygon-WF-Styles
# supplies the canonical statement template. Both are read at app import.
COPY --chown=judgehost:judgehost third_party ./third_party

ENV POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db \
    POLYGON_REPLICA_BARE_ROOT=/srv/polygon-replica/git \
    POLYGON_REPLICA_WORKSPACE_ROOT=/srv/polygon-replica/workspaces \
    POLYGON_REPLICA_ARTIFACTS_ROOT=/srv/polygon-replica/export \
    POLYGON_REPLICA_CACHE_ROOT=/tmp/polygon-replica \
    POLYGON_REPLICA_AUTH_COOKIE_SECURE=1

EXPOSE 8001

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/polygon-replica/scripts/docker-entrypoint.sh"]

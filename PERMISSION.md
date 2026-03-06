# PERMISSION.md

## Scope

This policy applies to work executed inside the WSL distro used for this repository.

- Distro: `PolygonReplica-Dev`
- Repo path: `/root/work/Polygon-Replica`

## Permission Model

Inside WSL, administrative/root privileges may be used when needed.

Hard boundary:

- Do not mutate anything under `/mnt/*`.

Allowed examples:

- Read from `/mnt/*`.
- Copy files from `/mnt/*` into Linux paths.
- Modify Linux-native paths (`/root`, `/tmp`, `/var`, repo files under `/root/work/Polygon-Replica`).

Forbidden examples under `/mnt/*`:

- `rm`, overwrite, rename, truncate
- `sed -i`
- `chmod`, `chown`
- destructive git operations targeting mounted paths

## Test Methods

```bash
cd /root/work/Polygon-Replica
source .venv/bin/activate
./scripts/test.sh
```

Focused test example:

```bash
python -m unittest tests.test_toolchain_languages -v
```

## Service Operations

```bash
cd /root/work/Polygon-Replica
source .venv/bin/activate
[ -f var/polygonlike.env ] && source var/polygonlike.env
./scripts/bootstrap_demo.sh
```

Default URL:

- `https://127.0.0.1:8001`

## Allowed Work Categories

- WSL environment provisioning
- code edits, checks, and tests in this repository
- local app server runs and artifact/log inspection

## Prohibited Work Categories

- any destructive change under `/mnt/*`
- any operation that can damage Windows-hosted mounted files

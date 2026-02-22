# Polygonlike Authoring System

This repository implements a local Polygon-like problem authoring system aligned with `AGENTS.md`:

- Git-backed per-problem repositories with per-user workspaces
- Local filesystem artifact store (`tests`, `ans`, `logs`, `statement_preview`, `export`, `manifest.json`)
- Minimum metadata database schema (`problems`, `users`, `repo_acl`, `workspaces`, `builds`, `runs`, `exports`, `audit_log`)
- Unified compiler layer with cache key `(toolchain_digest, source_hash)` and `testlib.h` include path support
- TeX preview compilation and log capture
- Build pipeline (`compile -> generate -> validate -> solve -> persist`)
- Runner page and exporter page
- Web UI sections: Files, Git, Build, Preview, Run, Export

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./scripts/bootstrap_demo.sh
```

Open: `http://127.0.0.1:8000`

## Notes

- Default host roots follow `AGENTS.md` (`/srv/git`, `/srv/workspaces`, `/srv/runs`, `/var/lib/polygonlike/artifacts`, `/var/cache/polygonlike`).
- For local dev without root paths, `scripts/bootstrap_demo.sh` maps all roots under `./var/`.
- The current runner supports pass-fail execution and stores placeholders for interactive transcript and multi-pass feedback flow.

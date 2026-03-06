# Polygon-Replica Problem Authoring System

本项目是本地部署的 Polygon-like 题目制作系统：

- Git 管理题目源
- 本地文件系统管理评测产物
- Web UI 完成主要工作流

## Core Capabilities

- Owner-scoped 问题模型：`<owner>/<slug>`
- End-to-end authoring：Statement / Files / Generators / Checker / Validator / Interactor / Tests / Solutions / Verifications / Packages
- Invocation backend：`auto` / `local-sandbox` / `domjudge-judgehost`
- ICPC 导出基于 committed `HEAD`

## Prerequisites

- `git`
- `g++` / `gcc`
- `python3`
- `openjdk` (`javac` + `java`)
- `tex-live`
- Linux `cgroups`/`rlimit`

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y git build-essential python3 python3-venv openjdk-17-jdk texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended util-linux
```

## Quick Start

```bash
source .venv/bin/activate
[ -f var/polygonlike.env ] && source var/polygonlike.env
./scripts/bootstrap_demo.sh
```

默认访问：`https://127.0.0.1:8000`

## Common Commands

```bash
# 本地开发服务
uvicorn app.main:app --reload --ssl-keyfile ./var/tls/dev-localhost.key --ssl-certfile ./var/tls/dev-localhost.crt

# 回归测试
./scripts/test.sh

# 全量 unittest
python -m unittest discover -s tests -p 'test_*.py' -v
```

## Workflow Notes

### Tests

- 元数据：`tests/spec.json`
- 核心字段：`id` / `kind` / `sample`
- 支持 sample 覆盖字段：`sample_input` / `sample_output` / `sample_output_validate`
- 负载：
  - `tests/manual/<id>.in`
  - `tests/generator/<id>.in`

### Invocations

- `run/new` 支持多选 `solutions` 与 `tests`
- 支持可选源码上传
- 不支持 custom path

### Packages

- 仅支持 ICPC 导出
- 导出基于 committed `HEAD`
- 同 revision 仅保留最后一次导出

## Sandbox

- 默认后端：`native-sandbox`
- 覆盖 `compile/build/run/preview`
- 使用 `rlimit + timeout + seccomp`
- 默认 deny-all 网络

## Runtime Paths

- bare repos: `/srv/git/<owner>/<slug>.git`
- workspaces: `/srv/workspaces/<viewer>/<owner>/<slug>/`
- runs: `/srv/runs/<run_id>/`
- judgehost temp: `/srv/runs/judgehost-domjudge/<task_id>/`
- artifacts: `/var/lib/polygonlike/artifacts/objects/<hh>/<ref>/`
- cache: `/var/cache/polygonlike`

## Active Root Docs

- `AGENTS.md`
- `PERMISSION.md`
- `BACKEND_TODO.md`
- `ASYNC_WORKER_PLAN.md`
- `PROGRESS.md`

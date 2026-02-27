# Polygon-Replica Problem Authoring System

本项目是一个本地部署的题目制作系统，目标是用 Git 驱动题目开发、用本地文件系统管理评测产物，并通过 Web UI 完成完整工作流。

核心特性：

- Git 作为题目源码唯一真相源（main-only）
- 本地产物存储（tests/ans/logs/preview/export）
- Tests / Invocations / Packages 的一体化流程（执行时按需生成可运行快照）
- 统一 `native-sandbox` 执行链路（compile/build/run/preview）

详细规范见：

- `AGENTS.md`

## Prerequisites

运行前请安装：

- `git`
- `g++` / `gcc`
- `python3`
- `openjdk` (`javac` + `java`)
- `tex-live`
- Linux `cgroups`/`rlimit` 支持

示例（Ubuntu/Debian）：

```bash
sudo apt-get update
sudo apt-get install -y git build-essential python3 python3-venv openjdk-17-jdk texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended util-linux
```

推荐直接运行安装脚本（会安装依赖、配置 userns、验证 `bwrap` 并创建 `.venv`）：

```bash
./scripts/install_host.sh
```

如果安装后启动仍报 `uid_map: Permission denied`，先检查：

```bash
sysctl -n kernel.unprivileged_userns_clone user.max_user_namespaces kernel.apparmor_restrict_unprivileged_userns
```

在 Ubuntu/AppArmor 环境下需要确保 `kernel.apparmor_restrict_unprivileged_userns=0`，否则无特权 `bwrap` 会被阻止。

如果 Statement 预览日志里出现 `I can't find the format file 'pdflatex.fmt'`，先执行：

```bash
fmtutil -user --byfmt pdflatex
```

如果你有 root 权限，也可以用系统级修复：

```bash
sudo fmtutil-sys --byfmt pdflatex
```

## Quick Start

```bash
source .venv/bin/activate
source var/polygonlike.env
./scripts/bootstrap_demo.sh
```

启动后访问：`https://127.0.0.1:8000`

## Common Commands

```bash
# 启动服务（示例，需 HTTPS 以携带 Secure 会话 Cookie）
uvicorn app.main:app --reload --ssl-keyfile ./var/tls/dev-localhost.key --ssl-certfile ./var/tls/dev-localhost.crt

# 全量 unittest
python -m unittest discover -s tests -p 'test_*.py' -v

# 常规回归（语法 + linter + dead code + unittest）
./scripts/test.sh

# 清理运行期缓存（保留 export/*.zip）
.venv/bin/python ./scripts/cleanup_runtime_cache.py
```

`./scripts/test.sh` 当前包含：

- `py_compile`
- `pyflakes`
- `vulture`（`--min-confidence 60`）
- `unittest`

## Workflow Notes

### testlib 维护策略

- `third_party/upstream/testlib/testlib.h` 是单独维护的 ICPC 兼容版本（交付内容的一部分）。
- 新建/补齐 workspace 时，`third_party/testlib/testlib.h` 从该维护版本拷贝。
- 维护目标：固定 `42/43` 语义，并直接兼容 ICPC output validator 风格调用（checker 可接收 `input/answer/feedback_dir`）。

### Tests

- Tests 元数据在 `tests/spec.json`，仅包含 `id/kind/sample`。
- manual 与 gen 负载分别存放于：
  - `tests/manual/<id>.in`
  - `tests/generator/<id>.in`

### Invocations

- `run/new` 支持多选 `solutions` 与 `tests`，并支持可选源码上传。
- 不再支持 custom path。
- Invocation 详情展示每测试点结果，包含 `ms` 与 `MB`，并支持展开查看输入/答案/输出预览。

### Packages

- 仅支持 ICPC 导出。
- 导出只基于当前 `HEAD` 的已提交 revision，不使用 working copy。
- 若当前 revision 缺少 committed build，Generate 会先构建再导出。
- 导出文件名统一为 `[problemid]-v[revision].zip`。
- 同一 revision 只保留最后一次导出结果。

## Authentication Note

登录/注册/改密请求不提交明文密码：前端提交 `sha256(csrf_token + password)`，并附带 verifier/proof 供后端校验。

## Sandbox Backend

系统使用单一后端：`native-sandbox`。

- 覆盖 `compile/build/run/preview`
- 使用 `rlimit + timeout + seccomp`
- 默认禁网（deny-all）

常用环境变量（示例）：

```bash
export POLYGONLIKE_SANDBOX_ROOT_SWITCH_TOOL=/usr/bin/bwrap
export POLYGONLIKE_RUN_MEMORY_MB=1024
export POLYGONLIKE_RUN_PROCESS_LIMIT=64
export POLYGONLIKE_RUN_OUTPUT_KB=65536
export POLYGONLIKE_COMPILE_TIMEOUT_SEC=120
export POLYGONLIKE_COMPILE_MEMORY_MB=2048
export POLYGONLIKE_COMPILE_PROCESS_LIMIT=0
export POLYGONLIKE_COMPILE_OUTPUT_KB=262144
export POLYGONLIKE_TEX_TIMEOUT_SEC=120
export POLYGONLIKE_TEX_MEMORY_MB=1024
export POLYGONLIKE_TEX_PROCESS_LIMIT=64
export POLYGONLIKE_TEX_OUTPUT_KB=131072
```

说明：

- `native-sandbox` 固定启用并强制要求 `bwrap` 换根能力；如果主机不支持，服务会直接失败（fail-closed），不会降级到弱隔离。

## Runtime Paths

默认路径：

- bare repos: `/srv/git`
- workspaces: `/srv/workspaces/<username>/<problem>/`
- run fallback root: `/srv/runs`（主要用于 `invalid-runs/`）
- artifacts: `/var/lib/polygonlike/artifacts`
- cache: `/var/cache/polygonlike`

## Project Layout

- `app/`: 后端服务与模板
- `scripts/`: 本地启动与测试脚本
- 根目录文档：`AGENTS.md` / `ASYNC_WORKER_PLAN.md` / `BACKEND_TODO.md` / `PROGRESS.md`
- `third_party/`: upstream 依赖资源
- `PROGRESS.md`: 当前里程碑状态

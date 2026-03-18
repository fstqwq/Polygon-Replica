# Polygon-Replica

本项目是一个本地部署的 Polygon-like 题目制作系统。核心约束很简单：

- Git 是题目源码的唯一真相来源
- 数据库只存元数据
- 题面、测试、导出包、运行产物都留在本地文件系统
- Web UI 是主要工作流入口

## 当前能力

- 题目工作区：`Statement / Files / Checker / Validator / Interactor / Generators / Tests / Solutions`
- 评测与验证：judgehost-only，异步任务通过 `/api/v4/*` 与 judgedaemon 交互
- 题目包：
  - 导入：`polygon` / `icpc` / `native`
  - 导出：`icpc` / `native`
- contest：
  - Polygon contest package 导入
  - contest-level PDF 编译
  - contest 公共文件与题目文件 override 处理
- 导入后的题目会自动生成初始 commit，不再停留在 `v0`

## 文档

- [INSTALL.md](/C:/code/Polygon-Replica/INSTALL.md): 安装与启动
- [AGENTS.md](/C:/code/Polygon-Replica/AGENTS.md): 仓库工程约束
- [PERMISSION.md](/C:/code/Polygon-Replica/PERMISSION.md): 权限模型
- [ASYNC_WORKER_PLAN.md](/C:/code/Polygon-Replica/ASYNC_WORKER_PLAN.md): 异步任务背景
- [BACKEND_TODO.md](/C:/code/Polygon-Replica/BACKEND_TODO.md): 现存后台问题
- [PROGRESS.md](/C:/code/Polygon-Replica/PROGRESS.md): 里程碑记录

## 快速开始

在 Linux / WSL 中：

```bash
./scripts/install_host.sh
source .venv/bin/activate
source /etc/polygon-replica.env
./scripts/start_local.sh
```

默认会启动 HTTPS 开发服务：

- `https://127.0.0.1:8000`

首次访问会进入 setup/login 流程。

## 常用命令

```bash
# 开发服务（HTTPS，自签名证书）
./scripts/start_local.sh

# 直接运行 uvicorn
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 仓库标准测试入口
./tests/scripts/test.sh

# 包导入 smoke
./tests/scripts/import-smoke.sh
```

## 运行时目录

- bare repo: `/srv/git/<owner>/<slug>.git`
- workspace: `/srv/workspaces/<viewer>/<owner>/<slug>/`
- run root: `/srv/runs/<run_id>/`
- judgehost temp root: `/srv/runs/judgehost-domjudge/<task_id>/`
- artifact root: `/var/lib/polygon-replica/artifacts/objects/<hh>/<ref>/`
- metadata DB: `problems/users/workspaces/verifications/exports/contest jobs/...`

默认运行时路径是绝对路径：

- `/var/lib/polygon-replica/metadata.db`
- `/srv/git`
- `/srv/workspaces`
- `/srv/runs`
- `/var/lib/polygon-replica/artifacts`
- `/var/cache/polygon-replica`

## 依赖说明

- Python 3 + venv
- Git
- TeX Live
- `bubblewrap` / `seccomp`
- 至少一个 DOMjudge judgedaemon，用于 runs / verifications / sample build

如果只启动 Web 服务而没有 judgedaemon：

- 页面能打开
- 题面编辑、文件编辑、Git 提交能工作
- verification / run / 部分 contest build 会停在队列里

## 测试说明

`tests/scripts/test.sh` 当前分 5 步：

1. `py_compile`
2. `pyflakes`
3. `vulture`
4. import/refactor policy checks
5. unit tests

默认跳过慢速 UI 测试；需要全量时：

```bash
POLYGON_REPLICA_INCLUDE_SLOW_TESTS=1 ./tests/scripts/test.sh
```

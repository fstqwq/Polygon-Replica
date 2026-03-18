# Installation

本文档描述当前仓库在 Debian / Ubuntu / WSL 上的安装方式。仓库自带的宿主机安装脚本是：

- [install_host.sh](/C:/code/Polygon-Replica/scripts/install_host.sh)

它是当前最可靠的安装入口。

## 1. 系统要求

最少需要：

- Linux 或 WSL
- `git`
- `python3`
- `python3-venv`
- TeX Live
- `bubblewrap`
- `libseccomp2`
- 允许 unprivileged user namespaces

当前安装脚本按 Debian / Ubuntu 编写。

## 2. 克隆仓库

```bash
git clone <your-repo-url> Polygon-Replica
cd Polygon-Replica
```

## 3. 安装系统依赖

推荐直接运行：

```bash
./scripts/install_host.sh
```

这会完成：

- 安装 apt 依赖
- 初始化 TeX formats
- 配置 user namespace sysctl
- 探测 `bubblewrap` 是否可用
- 探测 `pdflatex`
- 探测 T2A 编码支持
- 探测 `cm-super`
- 创建 `.venv`
- 安装 `requirements.txt`
- 写出 `/etc/polygon-replica.env`

如果你要手动装，当前脚本实际依赖这些包：

```bash
sudo apt-get update
sudo apt-get install -y \
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
```

如果缺少 TeX formats，可再执行：

```bash
sudo mktexlsr
sudo fmtutil-sys --byfmt pdflatex
```

## 4. 检查 user namespace / sandbox

安装脚本会写：

- `/etc/sysctl.d/99-polygon-replica-sandbox.conf`

目标值：

```text
kernel.unprivileged_userns_clone = 1
user.max_user_namespaces = 1048576
```

如果你的环境还有：

```text
kernel.apparmor_restrict_unprivileged_userns
```

脚本也会尝试把它设为 `0`。

如果这里被宿主机策略拦住，`bubblewrap` sandbox 不会正常工作，judge / TeX 路径都可能失败。

## 5. 激活虚拟环境

```bash
source .venv/bin/activate
source /etc/polygon-replica.env
```

`/etc/polygon-replica.env` 当前由安装脚本生成；如需自定义路径，可直接覆写环境变量。

## 6. 启动开发服务

推荐：

```bash
./scripts/start_local.sh
```

这个脚本会：

- 准备默认运行时目录
- 生成本地 TLS 自签名证书
- 设置默认运行时路径到系统绝对目录
- 启动 HTTPS uvicorn

默认地址：

- `https://127.0.0.1:8000`

默认映射路径：

- `POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db`
- `POLYGON_REPLICA_BARE_ROOT=/srv/git`
- `POLYGON_REPLICA_WORKSPACE_ROOT=/srv/workspaces`
- `POLYGON_REPLICA_RUN_ROOT=/srv/runs`
- `POLYGON_REPLICA_ARTIFACTS_ROOT=/var/lib/polygon-replica/artifacts`
- `POLYGON_REPLICA_CACHE_ROOT=/var/cache/polygon-replica`

你也可以直接运行 uvicorn：

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

但这样不会自动生成 TLS 证书，也不会帮你设默认 runtime paths。

## 7. judgedaemon / judgehost

这个仓库当前是 `judgehost-only` 模型。

这意味着：

- verification
- run
- sample build
- 部分 contest PDF 生成前置步骤

都依赖至少一个 DOMjudge judgedaemon 持续轮询本服务的 `/api/v4/*`。

如果没有 judgedaemon：

- Web UI 仍可使用
- Git/workspace/statement/files 流程可正常操作
- 但 verification / run / 部分导出与 contest build 会排队不完成

## 8. 首次使用

启动后，浏览器访问：

- `https://127.0.0.1:8000`

首次会进入 setup / login 流程。完成后即可：

- 创建问题
- 导入 Polygon / ICPC / native package
- 编辑题面与测试
- 启动 verification
- 导出 ICPC / native
- 导入 contest package 并生成 contest PDF

## 9. 测试

标准入口：

```bash
./tests/scripts/test.sh
```

默认跳过慢速 UI 集成测试。若要全量：

```bash
POLYGON_REPLICA_INCLUDE_SLOW_TESTS=1 ./tests/scripts/test.sh
```

也可以单独跑：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

## 10. 常见问题

### `bubblewrap probe failed`

说明 user namespace / seccomp / host policy 没满足要求。先检查：

- `kernel.unprivileged_userns_clone`
- `user.max_user_namespaces`
- AppArmor / container policy

### `missing LaTeX format pdflatex.fmt`

执行：

```bash
sudo mktexlsr
sudo fmtutil-sys --byfmt pdflatex
```

### `TeX T2A probe failed`

缺：

```bash
texlive-lang-cyrillic
```

### `TeX vector font probe failed`

缺：

```bash
cm-super
```

### verification / run 一直 queued

通常不是 Web 服务问题，而是 judgedaemon 没接上，或者 judgehost 凭据不对。

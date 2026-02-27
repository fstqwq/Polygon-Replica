# BACKEND_TODO

更新时间：2026-02-27

本文件仅保留“当前仍未完成且有工程价值”的后端事项，按优先级排序。

## P0 安全与隔离

1. 最小化 rootfs 绑定
- 现状：native-sandbox 启动已强制 `bwrap` root-switch（fail-closed）。
- 问题：运行时挂载集合仍偏宽泛，存在额外宿主信息暴露面。
- 目标：收敛到最小必要 bind 列表（编译/运行/TeX 各 profile 分离）。

2. seccomp 从“补丁式”向“最小权限”收敛
- 现状：已有限制，但未形成按任务类别精细化策略矩阵。
- 目标：为 compile/run/tex 维护独立 syscall allowlist（或稳定 deny matrix + 回归测试）。

3. cgroup v2 资源硬限制
- 现状：以 rlimit/timeout 为主。
- 目标：增加 memory/pids/cpu/io 的 cgroup v2 强约束与可观测指标。

## P1 任务队列可靠性

1. 队列持久化与重启恢复
- 现状：worker queue 已统一，但以进程内状态为主。
- 目标：queued/running job 可在进程重启后恢复或显式回收。

2. 队列背压与大请求内存控制
- 现状：可异步执行，但缺少稳定 backpressure 策略。
- 目标：队列容量上限、拒绝策略、上传内容临时文件落盘上限。

3. 任务可取消与超时收敛
- 现状：基础异步可用。
- 目标：支持 queued/running 的可控取消与统一超时治理。

## P1 结果可解释性

1. invocation 失败原因标准化
- 现状：前端已展示失败原因与部分 stderr，但不同失败路径格式不统一。
- 目标：统一 `error_code/message/stderr_excerpt` 数据结构并前后端对齐。

2. worker 观测增强
- 现状：worker 有进程内历史与快照能力，但尚未暴露稳定的外部运维端点。
- 目标：增加 job 级耗时分布、失败率、最近失败样本、队列拥塞指标。

## P2 格式与兼容验证

1. ICPC 导出一致性自动化
- 现状：可导出 ICPC 包。
- 目标：加入针对 upstream problem-package-format 的自动校验回归。

2. 标准组件导出语义回归
- 现状：checker 已支持 standard metadata 模式。
- 目标：为 standard checker/repository checker 两种模式补齐 exporter 回归。

## P2 数据与运维

1. DB 例行维护策略
- 现状：已讨论 WAL/vacuum 问题，尚缺完整作业编排与监控阈值。
- 目标：checkpoint + incremental/full vacuum 策略化、可观测、可手动触发。

2. 缓存/产物生命周期治理
- 现状：已有 `scripts/cleanup_runtime_cache.py`。
- 目标：形成周期任务、保留策略和清理审计记录。

## Done/移除说明

以下事项已不再作为 TODO：

- 继续维护 legacy 页面/路由兼容层。
- 单独 smoke_test 脚本链路（已切换为 `./scripts/test.sh` + unittest）。
- EJUDGE/CONTESTER 分支兼容型 testlib patch 流程。

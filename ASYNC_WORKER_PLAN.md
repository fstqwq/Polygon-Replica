# ASYNC_WORKER_PLAN

更新时间：2026-02-27

## 1. 目标

将 build/preview/run/export/verification 统一为可观测、可控制、可扩展的异步任务执行体系。

## 2. 当前已完成

1. 统一 worker 队列
- `app/services/worker_queue_service.py`
- 支持入队、去重 key、基础状态（queued/running/done/failed）

2. invocation backend 抽象
- `app/services/invocation_backend_service.py`
- 支持 `local-sandbox` 与 `domjudge-judgehost` 适配入口

3. 关键重任务已接入异步
- run execute batch
- verification start
- export create
- invocation 触发的批量执行队列

4. 内部状态快照
- worker queue service 提供进程内 snapshot 能力（尚未固化为稳定外部 API）

## 3. 当前短板

1. 队列耐久性不足
- 进程重启后 queued/running job 恢复策略不完整。

2. 任务治理能力不足
- 缺少统一 cancel 语义与资源配额策略。

3. 观测维度偏基础
- 目前主要是队列深度与运行状态，缺乏按 job_type 的时延/失败统计。

4. preview 仍为同步执行
- 语句编译请求会阻塞直至完成，尚未接入队列化执行。

## 4. 下一阶段计划

### Phase A: 可靠性

1. 将 job 元数据落库（或 durable log）以支持重启恢复。
2. 增加队列容量上限、背压策略、拒绝原因标准化。
3. 统一任务超时与取消（queued/running）。

### Phase B: 可观测

1. 记录 job 生命周期时间戳：enqueue/start/finish。
2. 增加失败原因分类：`queue_rejected` / `sandbox_error` / `compile_error` / `runtime_error` 等。
3. 对外提供稳定运维端点，并增加按 `job_type` 聚合统计。

### Phase C: 执行隔离一致性

1. 按任务类型定义 sandbox profile（compile/run/tex/export）。
2. 将实际生效限制参数回写到任务结果中，便于审计。

### Phase D: DOMjudge 深化

1. 固化 adapter I/O schema 与错误码规范。
2. 明确 remote id 与本地 run/invocation 的稳定映射。

## 5. 验收标准

1. HTTP 请求不阻塞重任务，均可追踪任务状态。
2. 重启后任务状态可解释（恢复/回收/失败原因明确）。
3. worker 状态可回答三类问题：
- 现在有什么任务在排队/运行
- 为什么失败
- 哪类任务正在退化（时延/失败率）

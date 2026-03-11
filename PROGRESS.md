## Judgehost Verification 全链路重构进度

Last updated: 2026-03-10

### 当前结论

- 主体方向保持不变：verification 走 judgehost 两类任务（`generate` / `solve`），`compile-only` 作为同级独立接口，仅供保存源码等编译检查入口。
- 本轮已修复一个关键错误：judgehost 拉取 testcase 文件时的 `testcase_id` 解析错位，导致拿错 input/answer，触发 `compare script ... exit 3`。
- 旧失败 invocation 仍会显示历史失败；修复后需要看新的 rejudge 结果。

### 已完成（Done）

1. 任务模型重构（同级三接口）
- `compile-only` 独立为任务模型（`task_kind=compile-only`），脚本链固定 `compile=<lang>.compile, run=skip.run, compare=skip.compare`。
- `generate` 与 `solve` 分离，verification 主流程不再应下发 compile-only。

2. judgehost 脚本资产收敛
- 脚本统一在 `app/service/judgehost/scripts/`：
  - `cpp.compile`, `java.compile`, `python.compile`
  - `normal.run`, `interactive.run`, `skip.run`
  - `normal.compare`, `generate.compare`, `skip.compare`

3. 哈希/缓存契约落地
- 文档化并落地 testcase 粒度缓存键与签名（见 `HASH_SCHEMA.md`）。
- `answer_hash` 参与 solve-verify 比较签名，保持逐 testcase 命中/失效。

4. 本轮关键修复（2026-03-10）
- 文件：`app/service/judgehost/internal/domjudge_result.py`
- 修复点：`domjudge_get_testcase_files(testcase_id)`
  - 先按 `judgehost_domjudge_cases.id`（case id）精确查询
  - 再回退 `testcase_id`
  - 最后回退内存 registry
- 目的：避免 judgedaemon 传入 case id 时误命中旧 registry，导致 input/answer 错配。

5. verification 构建路径防降级（2026-03-10）
- 文件：`app/impl/workspace/context_job_helper.py`
- 修复点：删除 `_ensure_implicit_build(..., for_verification=True)` 中对 `TypeError` 的静默回退逻辑。
- 变更前：异常时会回退 `run_build(problem, user)`，可能误走普通 build.compile 链路，出现 `Generate Inputs -> build failed: compile failed: validator`。
- 变更后：verification 只允许调用 `run_build(..., verification_pipeline=True)`，不再静默降级。

6. build 失败后禁止继续 verification solve（2026-03-10）
- 文件：`app/impl/workspace/context_job_helper.py`
- 背景：`inv-8de6be464f34` 对应 build `b-81212b53b43e` 在生成阶段失败（entry 5），但流程仍继续跑 solutions，导致后续使用残留/空输入，出现错误 AC。
- 修复点：`_ensure_implicit_build` 在触发 `run_build` 后强制检查 builds.status；仅 `ok` 允许继续，其它状态统一抛出 `build failed: ...` 并中断后续 solve。
- 结果：避免“Generate Inputs 已失败但 Run Solutions 继续执行”的不一致状态。

7. 同 build_ref 目录残留清理（2026-03-10）
- 文件：`app/service/build/runner.py`
- 背景：build_ref 复用时，旧构建残留可能污染新构建（失败构建中出现旧 tests/ans/logs）。
- 修复点：每次 build 启动先清空并重建 `tests/ans/logs/bin/export/statement_preview` 子目录。
- 结果：构建产物与本次执行一致，不再混入历史残留。

8. compare/compile 契约收敛与回归补齐（2026-03-10）
- 文件：
  - `app/service/judgehost/scripts/normal.compare`
  - `app/service/judgehost/scripts/python.compile`
  - `app/service/judgehost/internal/domjudge_result.py`
  - `tests/test_judgehost_service.py`
- 修复点：
  - `normal.compare` 改为严格 DOMjudge 约定：仅从 `stdin` 读取选手输出，不再把框架参数误透传给 checker。
  - `python.compile` 支持解释器回退链 `pypy3 -> python3 -> python`，避免 `ENTRY_POINT` 缺失类崩溃。
  - finalize 归一化修复：`compile_success=0` 时 run 状态统一落 `failed`，并保留 `CE` 与编译诊断。
  - 测试同步更新到新契约并修复 compile-only 失败路径断言。
- 验证：WSL 下 `python -m unittest tests.test_judgehost_service -v` 全部通过（70/70）。

### 本轮验证记录（Evidence）

- 历史失败：`inv-296d3e96683c`（修复前失败记录，保留为历史事实）。
- 修复后触发新 rejudge：`inv-9db01dc97dd5`
  - 不再出现此前的 `compare script ... exit 3` 这类错配崩溃信号。
  - `Run Solutions` 表格结果恢复到预期分布（AC/WA）。

### 未完成（Open）

1. 仍需完成四题完整验收（Playwright 强制）
- `admin/2024yunnan-matrix`
- `admin/taxi`
- `admin/2024hangzhou-rank-list-interactive`
- `admin/run-twice-guess-the-number`

2. verification 页面阶段汇总一致性
- 需要继续核对“阶段状态/构建日志/结果表”三者在失败与恢复路径下的一致性，避免出现“阶段显示失败但结果表已正常”的混合态。

3. native backend 清理收口
- 继续排查并移除所有非 TeX 代码执行路径残留。
- 测试侧仅保留 native TeX 最小覆盖。

### 下一步执行顺序

1. 先跑四题 Playwright 全链路验收（重启后清缓存）。
2. 针对每题保留 invocation 证据（run details + 关键日志）。
3. 修正 verification 阶段汇总一致性问题。
4. 清理 native 非 TeX 路径与过期测试，并回归核心测试集。

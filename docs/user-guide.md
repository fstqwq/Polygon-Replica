# Polygon Replica 用户指南

Polygon Replica 是一套面向 ICPC 风格题目的协作出题系统。它覆盖一道题从已有想法、初步解法和测试，到团队审阅、verification 与比赛包交付的过程。最终产物可按目标比赛系统的要求导出。

本指南假定读者用过 Codeforces Polygon，并熟悉桌面 agent。界面目前使用英文，文中沿用实际按钮和页面名称。

## Workspace、published revision 和 package

```text
创建或导入后，继续编辑题目
          |
          v
workspace（可变、每个用户彼此隔离）
          |-- statement preview
          |-- verification / custom run
          |
          | publish new revision
          v
published revision（团队共享的正式 Git 版本）
          |
          | package export
          v
native package（有共享 verification 认证，或 not verified）
          |
          | format adapter
          v
external packages（目标比赛系统格式）

contest = 有序题目列表 + 比赛属性 + 比赛题面模板
        = 下载时组合每道题当前 published revision 的包
```

- 题目编辑页的 `Save` 写入 workspace。`Properties` 和 `Statement Sources` 写入比赛的共享内容。
- Verification 针对启动时冻结的 workspace 快照。
- `Publish new revision` 创建团队共享的正式版本。
- Native package 以 published revision 为源码。要让新修改进入包，先 publish。
- External packages 直接从 native package 派生。

每位作者在独立的 Git workspace 中工作，published revision 是团队共享的基准。

## 页面入口

顶栏的题目和比赛入口分别是 `Problem` 和 `Contest`。账号与 agent 连接在右上角的 `Settings` 中。

| 操作 | 入口 |
| --- | --- |
| 查看、打开或创建题目 | `Problem` -> `My Problems` / `Open/Create` |
| 导入现有题目 | `Problem` -> `Import Problem Package` |
| 修改时限、内存、题型和 pass 数 | 题目 ID 旁的元数据胶囊/齿轮 |
| 审阅并发布改动 | 右侧 `Workspace` -> `Review workspace` |
| 处理已经发布的新版本 | 右侧 `Workspace` -> `Resolve Conflicts` |
| 直接处理源码树 | 右侧 `Workspace` -> `Browse Files` |
| 管理题目协作者 | 右侧 `Workspace` -> `Manage access` |
| 构建可交付包 | `Packages` |
| 创建和管理比赛 | `Contest` -> `My Contests` |
| 管理比赛协作者和逐题权限 | 比赛页右侧 `Manage access` |
| 修改登录密码 | `Settings` -> `User` |
| 连接桌面 agent | `Settings` -> `Agents` |

`My Problems` 显示最近的题目；知道完整 ID 时，也可以使用 `Open/Create` 打开。

## 一道题从新建到交付

### 1. 创建空题或导入已有题目

在 `Problem` 首页选择 `Open/Create`。输入一个不含 `/` 的题目短名称，系统会创建完整题目 ID `<你的用户名>/<短名称>`。打开其他人的题目时，输入完整题目 ID；你的账号还必须具备访问权限。

如果已经有包，选择 `Import Problem Package` 并上传 ZIP。系统会导入作者源码，建立初始 published revision 和 workspace。此后的修改进入 workspace；publish 后，再从 published revision 构建 package。

### 2. 设置题目运行模型

点击题目 ID 旁显示时限和内存的胶囊/齿轮，打开 `Edit metadata`。

- `Time Limit` 和 `Memory Limit` 定义最终判题限制。
- `Mode` 选择普通 `pass-fail` 或 `interactive`。
- `Pass Limit` 大于 1 时表示 multi-pass。

### 3. 写题面、组件、解法和测试

`Statements` 按语言维护 `Title`、`Legend`、`Input`、`Output`、`Notes`，交互题还会显示 `Interaction Protocol`。右上角的 `Preview: PDF HTML LaTeX` 用来检查最终渲染。题面编译使用的图片等资源放在 `Statement attachments`。需要随 ICPC 包交付给参赛者的文件放在 `Contestant attachments`。

`Tests` 中标记为 sample 的测试点会自动加入题面，也可以单独编辑题面中显示的输入和输出。样例的具体渲染方式由 `Statements` 中的 `Examples template`（`statement/examples.tex`）决定。默认实现已经分别处理普通输入输出、multi-pass、interactive 和 multi-pass interactive 样例；需要更高级的排版时，可以启用并编辑这份模板，并以默认实现为参考。

其余内容按页面职责填写：

- 普通题在 `Checker` 选择标准 checker，或编写 custom checker。interactive 题则在 `Interactor` 编写交互器。
- `Validator` 用来检查输入，`Generators` 保存生成器源码。
- 在 `Solutions` 中指定唯一的 `main correct solution (AC)`。其他解要尽量声明准确的 `Expected`，例如 `wrong_answer (WA)`、`time_limit_exceeded (TL)`、`run_time_error (RE)` 或 `compile_error (CE)`。
- `Tests` 中的顺序就是正式测试顺序。测试可以是 manual，也可以是一条调用已选 generator 的命令。

专用编辑页覆盖了日常工作。需要修改模板、描述文件或其他高级源码时，使用右侧的 `Browse Files`。题目源码采用固定目录结构，完整约定见[题目源码协议](protocol/problem-source.md)。

### 4. 构建交互题和 multi-pass 题目

#### 交互题

1. 在 `Edit metadata` 中将 `Mode` 设为 `interactive`。单次交互将 `Pass Limit` 设为 1；需要多次运行时设为允许的最大 pass 数。
2. 在 `Statements` 的 `Interaction Protocol` 中写清双方发送内容的格式、取值范围、查询次数限制、刷新要求和终止条件。
3. 在 `Interactor` 中编写 interactor。interactor 从测试输入读取隐藏数据，通过标准输入输出与 solution 通信，并负责给出 verdict。
4. 在 `Tests` 中准备 interactor 使用的隐藏输入，并在 `Validator` 中校验这些输入。在 `Solutions` 中准备 main correct solution 和需要检查的其他 solutions。
5. 运行 verification，检查所有测试上的交互记录、verdict、时间和内存。

Interactor 的 testlib 接口、单次交互模板和交互式 multi-pass 写法见 Polygon-Skills 的 [`polygon-interactor/SKILL.md`](https://github.com/fstqwq/Polygon-Skills/blob/master/polygon-interactor/SKILL.md)。需要向参赛者提供本地 testing tool 时，参考 [`testing_tool.md`](https://github.com/fstqwq/Polygon-Skills/blob/master/polygon-interactor/testing_tool.md)；题面中的交互协议措辞可参考 [`standard-sentences.md`](https://github.com/fstqwq/Polygon-Skills/blob/master/polygon-statement/references/standard-sentences.md)。

#### multi-pass 题目

在 `Edit metadata` 中把 `Pass Limit` 设为 2 或更大。非交互题保持 `Mode: pass-fail`；需要每个 pass 内继续交互时使用 `Mode: interactive`。

每个 pass 都会启动一个新的 solution 进程。第一个 pass 使用 `Tests` 中的测试输入：非交互题由 solution 读取，交互题由 interactor 读取。当前 pass 正确且还需要继续时，checker 或 interactor 写出 `nextpass.in`，作为下一个 pass 的测试输入。前一个 solution 进程的状态不会保留，后续阶段需要的信息必须能从新的输入和交互协议中恢复。

- 非交互 multi-pass 使用 custom checker。checker 先完整检查当前输出；需要继续时再写出 `nextpass.in` 并返回通过。具体写法见 [`polygon-checker/SKILL.md` 的 Option C](https://github.com/fstqwq/Polygon-Skills/blob/master/polygon-checker/SKILL.md#option-c-multi-pass-checker-non-interactive)。
- 交互式 multi-pass 由 interactor 完成当前 pass 的交互与判定，并为下一次运行生成 `nextpass.in`。具体写法见 [`polygon-interactor/SKILL.md` 的 Section B](https://github.com/fstqwq/Polygon-Skills/blob/master/polygon-interactor/SKILL.md#section-b-multi-pass)。

为 multi-pass 或交互题制作样例时，在 `Tests` 中使用 `Structured JSON` 按 pass 记录输入输出或交互事件；`Examples template` 的默认实现会按这种结构渲染题面。

### 5. 预览题面

题目页的 PDF、HTML、LaTeX 链接默认预览你自己的 workspace。`Packages` 的 revision 列表还可以预览某个已有 native package 中固定版本的题面。

Workspace preview 读取当前草稿。Package preview 读取所选 native package 中的固定版本。

### 6. 运行 verification

进入 `Verification`，点击 `Start verification`。系统会冻结当前 workspace，并为每个测试依次生成输入、生成答案，再运行其他 solutions。

| 任务 | 编译阶段 | 运行阶段 | 判定和产物 |
| --- | --- | --- | --- |
| 生成输入（`generate-input`） | 编译 generator | generator 使用测试命令运行两次 | 检测两次运行是否一致，然后运行 validator |
| 生成答案（`main-correct`） | 编译 main correct solution | main correct solution 读取该测试的输入 | 运行 `checker input output output`，即将 main correct 的输出与自身比较；通过后将该输出保存为 answer |
| 检查其他解法（`solution-run`） | 编译对应 solution | solution 读取同一份测试输入 | 运行 `checker input output answer`，其中 answer 来自 main correct |

将 generator 运行两次只能发现两次输出已经不同的明显非确定性，不能证明 generator 本身是确定的。例如，使用 `srand(time(NULL))` 时，两次运行可能取得相同的种子，因此不一定会被检查出来。

生成测试数据和使用 main correct solution 生成答案也属于评测任务，因此每个数据点产生的输入或答案都受任务输出大小限制。默认上限为 256 MB，超过部分会被截断；管理员可以调整这个限制。

完整 verification 还会运行以下 sanity check：

| 检查项 | 检查内容 | 未满足时 |
| --- | --- | --- |
| `Empty output stability` | 确认空输出不会被接受，也不会使 checker 或 interactor 产生 `FL` | sanity failed |
| `Unicode output stability` | 确认包含中文和 emoji 的无效输出能够被拒绝，而不会产生 `FL` | sanity failed |
| `Custom sample output` | 将 `Tests` 中启用 `Validate custom output` 的样例输出送入实际判题流程；如果同时填写了自定义样例输入，先用 main correct solution 生成对应 answer | sanity failed |
| `Summary runtime threshold` | 检查答案全部正确的 solution 的最大 user time；达到 time limit 的 50% 至 150% 时提示时限可能过紧 | warning |
| `Boundary coverage` | 根据 validator 产生的 testlib overview，检查所有测试是否覆盖了具有固定边界的变量最小值和最大值 | warning |

点击测试或单元格可查看 `Test Details`，下方 `Diagnostics` 汇总编译错误、生成失败和 sanity warning。Verification 状态含义如下：

- `queued` / `running`：任务正在等待或执行。
- `ok`：完整计划已经结束并符合预期。
- `failed`：generator、validator、main correct solution、运行环境或 expected mismatch 失败。
- `cancelled`：该 workspace 的 verification 所有者取消了任务。

需要只跑部分 `Solutions` / `Tests`，或临时上传一个源码时，选择 `Customize verification`，进入 `Run Solutions`。

默认情况下，执行内容完全相同的任务不会再次运行；你可以使用 `Rejudge Without Cache` 重复运行，以验证稳定性。

影响执行的配置、组件、`Solutions` 或 `Tests` 变化后，旧 verification 会显示 stale。

### 7. 审阅并 publish

右侧 `Workspace` 卡片会显示 workspace 和 published revision 是否一致，以及当前有哪些未发布文件。点击 `Review workspace`：

1. 在 `Review` 中检查 `Published`、`Workspace`、`Verification` 和 `Content` 状态。
2. 在 `File Changes` 中逐项审阅差异；不想保留的单文件修改可以 `Discard file changes`。
3. 填写有意义的 `Message`，点击 `Publish new revision`。

如果另一位作者已经发布了新版，workspace 会提示 `Resolve Conflicts`，并禁止直接发布。进入 `Review Published Changes`，可以采用建议合并结果，也可以逐个文件选择保留 workspace 或 published 版本。应用以后再次审阅差异，再 publish。

### 8. 创建 native package 和外部包

进入 `Packages`。如果页面显示 `No published revision is available.`，先发布 workspace。默认创建流程会复用已有 verified native package。如果没有，系统会先运行 verification，再创建或认证当前 published revision 的 native package。

勾选 `Run standard solution only` 后，只执行测试输入生成和 main correct solution，并创建或复用一个 `not verified` native package。正式交付前，可由具备权限的作者使用默认流程或 `Verify` 补齐认证。

外部包从同一个 native package 派生。当前支持的格式及其题型、pass 数、checker 和内存范围见[包导入与导出协议](protocol/package.md)。如果 adapter 拒绝当前配置，请按目标系统限制调整。

## 使用桌面 agent

安装 [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) 后，你可以通过桌面 agent 操作题目内容，并连接 Polygon Replica 完成同步、verification、导出和发布。连接和撤销入口在右上角 `Settings` -> `Agents`。

本地编译和运行适合快速迭代。最终时限、性能和 verdict 仍以 Polygon Replica 的 `Verification` 为准。

### 连接与授权

1. 登录 Polygon Replica，进入 `Settings` -> `Agents`。
2. 点击 `Connect to Agent`，复制页面生成的 registration URL。这个 URL 只能使用一次，页面会显示其过期时间。
3. 把完整 URL 发给桌面 agent，让它使用 `polygon-agent-auth` 连接。这个地址是 agent 注册端点，不要当作普通网页手工打开。
4. 注册成功后，`Agents` 页面会出现会话卡，显示 agent 名称、`Desktop ID`、连接时间、`Last seen` 和权限。
5. `General permission` 是整个 agent 会话的基础权限。新会话默认为 `none`，agent 需要逐题申请授权。改为 `readonly`、`workspace` 或 `commit` 后，该 scope 会应用到你的账号当前有权访问的所有题目，直到再次修改或断开会话；它不会超过你自己的 problem 权限。
6. Agent 首次处理某道题或需要更高权限时，会给出 approval URL。请使用连接该 agent 的同一账号打开它。核对 agent、`Desktop ID`、problem 和 scope 后，再选择有效期并 `Approve` 或 `Deny`。

每条授权都有自己的有效期和撤销状态。某条授权到期或被 `Revoke` 后，它不再贡献权限。如果 `General permission` 或同一问题的另一条有效授权仍然够用，agent 还可以继续操作。

### 选择合适的 scope

| scope | agent 可以做什么 | 不能做什么 |
| --- | --- | --- |
| `none`（`General permission`） | 不预授予跨题能力；需要时逐题申请 | 不能仅凭会话访问任意题目 |
| `readonly` | 读取 workspace、查看状态、下载/比较快照、启动和检查标准 verification、读取已有成果 | 不能修改远端 workspace、启动新的导出或 publish |
| `workspace` | 包含 readonly；把本地修改应用到 workspace，上传/删除文件，启动账号有权进行的导出 | 不能 publish 正式 revision |
| `commit` | 包含 workspace；在明确要求下 commit/publish；满足用户权限时可让 verification 成为共享包认证证据 | 不会获得 problem 管理权、成员管理权或浏览器提升权限 |

单独读取文件或 snapshot 只需要 `readonly`。当前 Polygon-Skills 的完整 `clone` 和日常本地镜像流程会申请 `workspace`，以便继续编辑和 push。若只想让 agent 查看状态、读取文件或检查已有结果，授予 `readonly` 即可。

有两个操作必须使用 `General permission`。创建自己命名空间下的新题时，该权限需要设为 `commit`；按 contest 的题目列表拉取整场题目时，该权限至少需要设为 `readonly`，同时你的账号必须拥有该 contest 的 read 权限。逐题 grant 不能代替这两项权限。

### Polygon-Skills 命令

| skill | 用途 |
| --- | --- |
| `polygon-init` | 创建题目源码结构 |
| `polygon-statement` | 编写题面 |
| `polygon-validator` | 编写 validator |
| `polygon-checker` | 选择或编写 checker |
| `polygon-interactor` | 编写 interactor 和 testing tool |
| `polygon-solution` | 编写正确解与错误解 |
| `polygon-hack` | 设计针对错误解的测试 |
| `polygon-generate-tests` | 设计并生成测试数据 |
| `polygon-review` | 完整审阅题目 |
| `polygon-agent-auth` | 连接 Polygon Replica，并申请题目授权 |
| `polygon-agent-pull` | 将远端 workspace 拉取为本地题目镜像 |
| `polygon-agent-push` | 将本地题目镜像应用到远端 workspace |
| `polygon-agent-verification` | 启动、等待并检查 verification |
| `polygon-agent-export` | 创建并下载 package |
| `polygon-agent-commit` | publish 当前 workspace |
| `polygon-workspace-snapshot-export` | 导出可传递的 workspace snapshot |
| `polygon-workspace-snapshot-import` | 从 workspace snapshot 恢复本地题目 |

### 本地与远端 Git 历史

Polygon-Skills 有意将 agent 的本地 Git 历史与 Polygon Replica 的远端 Git 历史分开。Agent 每完成一次本地操作，都会被要求创建一个单独的 commit，作为可以撤销的恢复点；这些本地 commit 不会直接成为远端的 published revision。

远端发生修改后，agent 在同步前也会先提交尚未提交的本地改动，再拉取并协调远端变化。这样，本地编辑、同步到 workspace 和 publish 始终是相互独立且可以审阅的步骤。

要收回权限，可以在 `Settings` -> `Agents` 中：

- 将 `General permission` 调低或设回 `none`；这不会自动删除仍有效的逐题 grant。
- 在 `Authorized Problems` 中 `Revoke` 某一条 grant。同一问题可能有多条授权；要完全收回该题权限，还要确认没有其他有效 grant，并且 general permission 没有覆盖它。
- 点击 `Disconnect Agent` 删除整个会话及其请求和 grants，使原凭据失效。

## 多人协作与权限

每位用户都有独立的 workspace。A 尚未 publish 的修改，B 看不到。A publish 后，B 会看到 published revision 已更新，需要先处理更新或冲突才能继续发布。

Problem 权限如下：

| 操作 | `read` | `write` | `owner` |
| --- | --- | --- | --- |
| 阅读题目；预览自己的 workspace 或已有包 | 可以 | 可以 | 可以 |
| 启动、查看和重新判可见的 verification；下载成功包 | 可以 | 可以 | 可以 |
| 编辑自己的 workspace；使用 `Custom Run`；创建包；publish | — | 可以 | 可以 |
| 通过 `Manage access` 管理其他人的直接 problem `read` / `write` | — | 可以 | 可以 |
| 删除 problem | — | — | 可以 |

如果按钮只读、禁用或不存在，先检查当前角色。部分禁用控件会在悬停时显示原因。

Contest 权限如下：

| 操作 | `read` | `write` | `owner` |
| --- | --- | --- | --- |
| 查看 contest | 可以 | 可以 | 可以 |
| 从 contest 页面进入某道题 | 需要该题直接 problem `read` | 相同 | 相同 |
| 编辑某道题源码 | 需要该题直接 problem `write` 或 `owner` | 相同 | 相同 |
| 编辑比赛属性和比赛级题面 | — | 可以 | 可以 |
| `Build All Packages` 或批量调整 TL/ML | — | 只处理直接可写的题 | 相同 |
| 修改 idx 和题目顺序 | — | 可以 | 可以 |
| 移除任意题目 | — | 可以 | 可以 |
| 加入题目 | — | 需要该题的直接 problem `write` 或 `owner` | 需要该题的直接 problem `write` 或 `owner` |
| 通过 `Manage access` 管理其他人的 contest membership | — | 可以 | 可以 |
| 退出 contest | 可以 | 可以 | — |
| 在权限矩阵中管理某道题的直接 problem 权限 | — | 还需要该题的直接 problem `write` 或 `owner` | 相同 |
| 整场题面预览；下载已完成的比赛包 | 每道题都需要直接 problem `read` | 相同 | 相同 |

Problem 和 contest 的 owner 身份固定，不能在普通角色表里转让。用户不能给自己改角色，但 contest 的 `read` 和 `write` 成员可以在 `Actions` 中点击 `Exit`，移除自己的 contest membership。系统管理员拥有完整权限。

Contest membership 和 problem 权限彼此独立。获得 contest `read` 或 `write` 不会自动获得其中任何题目的权限。比赛的 `Manage access` 页面提供 `problem × user` 矩阵，修改的是全局、直接的 problem ACL：成员退出 contest 或题目被移出 contest 后，这些权限仍然保留，必须显式撤销。矩阵只列出当前比赛成员；其他用户仍在单题的 `Manage access` 页面管理。

## 组一场比赛

### 创建或导入比赛

进入 `Contest` -> `My Contests`。`Create Contest` 用于创建空比赛。选择 `Import Polygon Contest Package` 可以上传 Polygon contest ZIP。随后在 `Review Contest Import` 检查比赛短名称、标题和每道题的新短名称，再确认导入。

### 编排题目

`Problems` 概览列出每题的题号（idx）、时限、内存、题型、内容就绪情况，以及 `Workspace`、`Verification` 和 `Package` 状态。点击 `Manage problems` 可以搜索、加入、移除和排序题目，也可以批量调整时限/内存；具体授权条件见上表。

题目加入成功后，页面会转到 `Manage access`，并高亮新题对应的行。新增或更新 contest member 后，同一页面会高亮该成员列。此时可以在矩阵里一次分配多道题的直接 `read` / `write`；行首和列首的批量选择只填写当前操作者有权管理的单元格，最后仍要点击 `Save Problem Access`。Contest membership 本身不授予题目权限。

### 预览整场题面

`Properties` 用于维护比赛属性及其多语言内容，`Statement Sources` 用于编辑整场题面的 TeX 模板和资源。banner、奇数题后插入空白页等选项也在这里。

比赛页右侧的 `Statements (HTML, <Language>)` 和 `Statements (PDF, <Language>)` 按题号顺序预览整场题面。`Workspace` 来源用于查看各题 workspace 中的内容，`Packages` 来源用于查看各题 native package 中的内容。你需要对所有题目具有 read 权限，并且所有题目都具备所选来源和共同语言，对应的预览链接才会显示。

### 构建和下载比赛包

点击 `Build All Packages` 后，系统会为你有权构建且当前包未 `ready` 的题目排队。所有题的当前包都变为 `ready` 后，页面才显示 `Download Packages`。

下载时选择一种 `External format`。系统准备缺少的目标格式包，再按题号返回整场归档。归档根目录同时包含所有题共同具备的各语言完整题面 PDF。如果某道题不符合所选格式，页面会显示需要调整的题目配置。

## 常见状态和处理方法

| 现象 | 这通常意味着什么 | 去哪里处理 |
| --- | --- | --- |
| 保存后其他人看不到修改 | 修改还在 workspace | `Review workspace` -> `Publish new revision` |
| `Resolve Conflicts` 出现，publish 不可用 | published revision 已由其他作者更新 | `Review Published Changes`，应用并复查合并结果 |
| `Verification` 显示 stale | 运行相关源码、配置或测试在 verification 后发生变化 | 对当前 workspace 重新 `Start verification` |
| `Verification` 为 `failed`，但结果矩阵看不出原因 | 可能是生成、校验、main correct、编译、证据或 sanity 阶段失败 | 打开该次 verification 的 `Reason`、`Test Details` 和 `Diagnostics` |
| `Packages` 显示没有 published revision | 当前题还没有正式版本 | 到 `Review workspace` 点击 `Publish new revision` |
| 发布新 revision 后，package 显示 `stale`/`none` | 旧包属于旧 revision | 在 `Packages` 为当前 revision 创建新包 |
| package 是 `not verified` | 当前 native package 没有与之匹配的共享 verification 认证 | 有相应权限的作者可通过 `Verify` 或默认流程补齐认证 |
| 比赛没有整场题面预览链接 | 某题无权访问、缺所选来源，或各题没有共同语言 | 检查每题访问权、workspace/package 和语言 |
| 比赛不能 `Download Packages` | 并非所有当前包都 ready | 先 `Build All Packages`，查看失败题的 `Packages` |
| registration URL 不存在、过期或已使用 | 注册 URL 只能使用一次，并且有效期较短 | 在 `Settings` -> `Agents` 重新点击 `Connect to Agent`，把新 URL 发给 agent |
| approval URL 返回 404 或显示 expired | 当前登录账号与 agent 所连接的账号不一致，或授权请求已过期、会话已不存在 | 先确认登录账号，再让 agent 重新请求该题 scope |

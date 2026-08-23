# Polygon Replica 用户指南

Polygon Replica 是一套面向 ICPC 风格题目的协作出题系统。它覆盖一道题从已有想法、初步解法和测试，到团队审阅、完整验证与比赛包交付的过程。比赛当天的报名、提交与榜单不在这里运行。最终产物可按目标比赛系统的要求导出。

本指南假定读者用过 Codeforces Polygon，并熟悉桌面 Agent。界面目前使用英文，文中沿用实际按钮和页面名称。

## Workspace、Published Revision 和 Package

```text
创建或导入后，继续编辑题目
          |
          v
我的 Workspace（可变、每个用户彼此隔离）
          |-- Statement Preview
          |-- Verification / Custom Run
          |
          | Publish new revision
          v
Published Revision（团队共享的正式 Git 版本）
          |
          | Package Export
          v
Native Package（有共享验证认证，或 not verified）
          |
          | Format Adapter
          v
External Package（目标比赛系统格式）

Contest = 有序题目列表 + 比赛属性 + 比赛题面模板
        = 下载时组合每道题当前 Published Revision 的包
```

- 题目编辑页的 `Save` 写入个人 Problem Workspace。Contest Properties 和 Statement Sources 写入比赛的共享内容。
- `Verification` 针对启动时冻结的 Workspace 快照。之后的编辑不会改变这次结果，验证也不会发布源码。
- `Publish new revision` 创建团队共享的正式版本。验证与发布是两个独立动作，Publish 本身不能证明题目正确。
- Native Package 以 Published Revision 为源码。要让新修改进入包，先 Publish。
- External Package 直接从 Native Package 派生。
- 生成的测试输入、标准答案、PDF、运行日志和包都属于产物。作者源码仍需单独保留。

每位作者在独立的 Git Workspace 中工作，Published Revision 是团队共享的基准。Verification 和 Package 保留各自对应的源码状态，因此多人和 Agent 并行工作时仍可追溯结果。

## 页面入口

顶栏的主入口是单数形式的 `Problem` 和 `Contest`。右上角的 `Settings` 用于账号与 Agent 连接。

| 想做的事 | 从哪里进入 | 你会得到什么 |
| --- | --- | --- |
| 查看、打开或创建题目 | `Problem` -> `My Problems` -> `Open/Create` | 打开完整 Problem ID，或创建自己的新题 |
| 导入现有题目 | `Problem` -> `Import Problem Package` | 从 Polygon Linux、ICPC 或 Polygon Replica 包创建新题 |
| 修改时限、内存、题型和 pass 数 | 打开题目，点击题目 ID 旁的元数据胶囊/齿轮 | `Time Limit`、`Memory Limit`、`Mode`、`Pass Limit` |
| 写题面并预览 | `Statements` | 多语言题面、PDF、HTML、LaTeX 预览和附件 |
| 选择或编写输出校验 | `Checker` | 标准 testlib checker 或自定义 checker |
| 编写交互器 | interactive 题目的 `Interactor` | 交互协议对应的 interactor 源码 |
| 编写输入校验器和生成器 | `Validator`、`Generators` | validator、generator 及其选择关系 |
| 管理标准解、错解和慢解 | `Solutions` | 解法源码及各自的 Expected 行为 |
| 管理测试和样例 | `Tests` | 手工测试、生成命令、顺序、样例展示数据 |
| 做全量验证或定点调试 | `Verification` | 结果矩阵、测试详情、编译与运行诊断 |
| 审阅并发布自己的改动 | 右侧 `Workspace` -> `Review workspace` | 文件差异、发布消息和正式 revision |
| 处理其他人已经发布的新版本 | 右侧 `Workspace` -> `Resolve Conflicts` | 建议合并结果或逐文件取舍 |
| 直接处理源码树 | 右侧 `Workspace` -> `Browse Files` | 文件浏览、编辑、上传、下载、重命名和删除 |
| 管理题目协作者 | 右侧 `Workspace` -> `Manage access` | Problem 的 read/write 成员 |
| 构建可交付包 | `Packages` | Native Package 和目标平台包 |
| 组一场比赛 | `Contest` -> `My Contests` | 有序题目列表、整场题面预览和比赛包 |
| 管理比赛协作者 | 比赛页右侧 `Manage access` | Contest 的 read/write 成员 |
| 修改登录密码 | `Settings` -> `User` | 当前账号的密码设置 |
| 连接桌面 Agent | `Settings` -> `Agents` | Agent 会话、权限和逐题授权 |

`My Problems` 只显示配置数量以内的最近条目。题目没有出现在列表里，不代表它不存在。知道完整 ID 时，仍可使用 `Open/Create` 打开。

## 一道题从新建到交付

### 1. 创建空题或导入已有题目

在 `Problem` 首页选择 `Open/Create`。输入不带 `/` 的新 slug，系统会创建 `<你的用户名>/<slug>`。打开其他人的题目时，优先使用完整的 `<owner>/<slug>`。你的账号还必须具备访问权限。

如果已经有包，选择 `Import Problem Package` 并上传 ZIP。当前首页入口会把外部包规范化为本系统的作者源码，导入为新题，同时建立初始 Published Revision 和可编辑 Workspace。上传归档不会被当作本系统已有的 Native Package，也不会继承原有认证。此后的修改仍先进入个人 Workspace，可在其中继续编辑并按需运行 Verification。Publish 后，再从 Published Revision 构建 Package。

### 2. 设置题目运行模型

点击题目 ID 旁显示时限和内存的胶囊/齿轮，会打开 `Edit metadata` 弹窗。本系统没有单独的 `General` 标签。

- `Time Limit` 和 `Memory Limit` 定义最终判题限制。
- `Mode` 选择普通 `pass-fail` 或 `interactive`。
- `Pass Limit` 大于 1 时表示 multi-pass。

### 3. 写题面、组件、解法和测试

`Statements` 按语言维护 Title、Legend、Input、Output、Notes，交互题还会显示 Interaction Protocol。右上角的 `Preview: PDF HTML LaTeX` 用来检查最终渲染。题面编译使用的图片等资源放在 `Statement attachments`。需要随 ICPC 包交付给参赛者的文件放在 `Contestant attachments`。

样例在 `Tests` 中维护，不在 `Statements` 中维护。将一个测试标成 sample 后，可以使用默认内容、单独填写 Input/Output，或为 multi-pass、interactive 样例提供 Structured JSON。回到 `Statements` 预览时，系统会把这些样例投影到题面中，但不会把生成出的样例内容写回作者源码。

其余内容按页面职责填写：

- 普通题在 `Checker` 选择标准 checker，或编写 custom checker。interactive 题则在 `Interactor` 编写交互器。
- `Validator` 用来检查输入，`Generators` 保存生成器源码。只有被明确选择的组件才参与构建，系统不会因为某个文件名看起来像 `validator.cpp` 或 `gen.cpp` 就自动使用它。
- 在 `Solutions` 中指定唯一的 `main correct solution (AC)`。其他解要尽量声明准确的 Expected，例如 `wrong_answer (WA)`、`time_limit_exceeded (TL)`、`run_time_error (RE)` 或 `compile_error (CE)`。
- `Tests` 中的顺序就是正式测试顺序。测试可以是 manual，也可以是一条调用已选 Generator 的命令。系统不会扫描目录并猜测哪些文件是测试，至少需要一个显式测试。

保存 checker、validator、generator 或 solution 只会写入 Workspace，不会在保存时自动编译。语法、工具链和运行错误由后续 Verification 或 Package 构建报告。

专用编辑页覆盖了日常工作。需要修改模板、描述文件或其他高级源码时，使用右侧的 `Browse Files`。题目源码采用固定目录结构，完整约定见[题目源码协议](protocol/problem-source.md)。

### 4. 预览题面

题目页的 PDF、HTML、LaTeX 链接默认预览你自己的 Workspace。`Packages` 的 revision 列表还可以预览某个已有 Native Package 中固定版本的题面。

Workspace Preview 读取当前草稿。失败时，按页面中的转换错误或日志修复 Workspace 后重试。PDF 还会突出首个 LaTeX 错误。

Package Preview 读取固定的 Native Package，不会触发 Build、Verify 或 Publish。要查看修复后的 Package Preview，需要修改 Workspace、Publish 新 revision，并构建新的 Native Package。

### 5. 运行 Verification

进入 `Verification`，点击 `Start verification` 运行标准全量验证。系统会对当前 Workspace 冻结一份快照，并为每个测试建立依赖链：读取 manual input，或运行 Generator 得到输入；配置了 Validator 时，检查准备好的测试输入；随后运行 main correct solution 取得答案，再运行其他 Solutions，并用 Checker 或 Interactor 判断结果。不同测试和彼此独立的任务可以并发执行。

详情页按“测试为行、解法为列”展示 verdict、时间和内存。点击测试或单元格可查看 `Test Details`，下方 `Diagnostics` 汇总编译错误、生成失败和 sanity warning。验证状态含义如下：

- `queued` / `running`：任务正在等待或执行。
- `ok`：完整计划已经结束并符合预期，但仍可能附带值得处理的 warning。
- `failed`：可能是 Generator、Validator、main correct solution、运行环境或 Expected mismatch 失败，不能只按“某个解 WA”理解。
- `cancelled`：该 Workspace 的验证所有者取消了任务。

需要只跑部分 Solutions/Tests，或临时上传一个源码时，选择 `Customize verification`，进入 `Run Solutions`。这是调试工具，不等价于 Full Verification。怀疑执行缓存掩盖了问题时，可以使用 `Rejudge Without Cache`。普通改题循环不需要每次绕过缓存。

影响执行的配置、组件、Solutions 或 Tests 变化后，旧验证会显示 stale。只改题面文本不会让执行验证 stale。旧记录仍说明当时冻结快照的结果，不会自动升级为新 Workspace 的证据。

### 6. 审阅并 Publish

右侧 `Workspace` 卡片会显示你的 Workspace 和 Published Revision 是否一致，以及当前有哪些未发布文件。点击 `Review workspace`：

1. 在 `Review` 中检查 Published、Workspace、Verification 和 Content 状态。
2. 在 `File Changes` 中逐项审阅差异；不想保留的单文件修改可以 `Discard file changes`。
3. 填写有意义的 `Message`，点击 `Publish new revision`。

`Publish` 不要求已有成功的 Verification。团队可以在发布前对 Workspace 做 Full Verification。默认 Package 流程则会为 Published Revision 准备匹配的完整验证证据。

如果另一位作者已经发布了新版，你的 Workspace 会提示 `Resolve Conflicts`，并禁止直接发布。进入 `Review Published Changes`，可以采用建议合并结果，也可以逐个文件选择保留 Workspace 或 Published 版本。应用以后再次审阅差异，再 Publish。

### 7. 创建 Native Package 和外部包

进入 `Packages`。如果页面显示 `No published revision is available.`，先回到 Workspace Publish。默认创建流程会复用已有 verified Native Package。如果没有，系统会先做完整验证，再创建或认证当前 Published Revision 的 Native Package。

勾选 `Run standard solution only` 后，需要构建时只执行测试输入生成和 main correct solution，并创建或复用一个 `not verified` Native Package。这个状态只描述共享认证：归档本身仍可用，私有 Workspace 中也可能已有完整验证记录。正式交付前，可由具备权限的作者使用默认完整流程或 `Verify` 补齐认证。

外部包从同一个 Native Package 派生。当前支持的格式及其题型、pass 数、checker 和内存范围见[包导入与导出协议](protocol/package.md)。如果 Adapter 拒绝当前配置，请按目标系统限制调整。

发布新 revision 不会删除或改写旧包。仍然可用的历史 Native Package、对应题面预览和已经生成的外部包会继续列在 `Revisions` 中。当前 revision 需要属于自己的新包。

## 使用桌面 Agent

你仍然可以在浏览器里完成整套出题流程。桌面 Agent 主要在本地编辑和比较源码，再把改动同步到你在 Polygon Replica 中的 Workspace。Verification、Package Export 和 Publish 都由 Polygon Replica 执行。Agent 只负责按你的授权发起任务、查看状态或下载结果。连接和撤销入口在右上角 `Settings` -> `Agents`，问题页没有单独的 Agent 标签。

### 准备 Polygon-Skills

推荐为桌面 Agent 安装 [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills)。它既能辅助编写题面、组件、解法和测试，也封装了连接 Polygon Replica、同步 Workspace、运行 Verification、导出和发布的流程。Codex 用户可按该仓库的说明，把这些技能放入工作目录的 `.codex/skills/`。

本地编译和运行适合快速迭代。最终时限、性能和 verdict 仍以 Polygon Replica 的 `Verification` 为准。

### 连接与授权

1. 登录 Polygon Replica，进入 `Settings` -> `Agents`。
2. 点击 `Connect to Agent`，复制页面生成的 Registration URL。这个 URL 只能使用一次，过期时间以页面显示为准。
3. 把完整 URL 发给桌面 Agent，让它使用 `polygon-agent-auth` 连接。这个地址是 Agent 注册端点，不要当作普通网页手工打开。
4. 注册成功后，`Agents` 页面会出现会话卡，显示 Agent 名称、Desktop ID、连接时间、Last seen 和权限。
5. `General permission` 是整个 Agent 会话的基础权限。新会话默认为 `none`，Agent 需要逐题申请授权。改为 `readonly`、`workspace` 或 `commit` 后，该 Scope 会应用到你的账号当前有权访问的所有题目，直到再次修改或断开会话；它不会超过你自己的 Problem 权限。
6. Agent 首次处理某道题或需要更高权限时，会给出 approval URL。请使用连接该 Agent 的同一账号打开它。核对 Agent、Desktop ID、Problem 和 Scope 后，再选择有效期并 `Approve` 或 `Deny`。

逐题授权可选择 1 小时、24 小时、7 天、30 天或 forever。每条授权都有自己的有效期和撤销状态。某条授权到期或被 `Revoke` 后，它不再贡献权限。如果 General permission 或同一问题的另一条有效授权仍然够用，Agent 还可以继续操作。你的账号一旦失去该题访问权，Agent 会在下一次请求时立即失权。

### 选择合适的 Scope

| Scope | Agent 可以做什么 | 不能做什么 |
| --- | --- | --- |
| `none`（General permission） | 不预授予跨题能力；需要时逐题申请 | 不能仅凭会话访问任意题目 |
| `readonly` | 读取 Workspace、查看状态、下载/比较快照、启动和检查标准 Verification、读取已有成果 | 不能修改远端 Workspace、启动新的导出或 Publish |
| `workspace` | 包含 readonly；把本地修改应用到你的 Workspace，上传/删除文件，启动账号有权进行的导出 | 不能 Publish 正式 revision |
| `commit` | 包含 workspace；在明确要求下 Commit/Publish；满足用户权限时可让 Full Verification 成为共享包认证证据 | 不会获得 Problem 管理权、成员管理权或浏览器提升权限 |

单独读取文件或 snapshot 只需要 `readonly`。当前 Polygon-Skills 的完整 `clone` 和日常本地镜像流程会申请 `workspace`，以便继续编辑和 push。若只想让 Agent 查看状态、读取文件或检查已有结果，授予 `readonly` 即可。

有两个操作必须使用 General permission。创建自己命名空间下的新题需要 General `commit`；按 Contest 的题目列表拉取整场题目需要 General `readonly`，同时你的账号必须拥有该 Contest 的 read 权限。逐题 grant 不能代替这两项权限。

### Agent 与浏览器如何配合

```text
桌面 Agent 的本地目录
          | pull / clone
          v
本地题目镜像
          | push / apply
          v
你在 Polygon Replica 中的 Workspace ------> Full Verification
          |                                  （冻结快照；不发布源码）
          | Publish / commit
          v
Published Revision
          | export
          v
Native Package -> 外部平台包
```

本地目录不是远端 Workspace 的实时挂载。Agent 需要先 pull，在本地完成修改，再 push 到你的 Workspace。push 只更新 Workspace，并不会 Publish。之后可以回到浏览器，在 `Review workspace`、`Verification` 和 `Packages` 中复查结果。

`push` 发送的是题目作者源码的完整镜像。Polygon Replica 会先比较，再一次应用。远端存在而本地镜像缺少的作者文件会被删除。`.git/`、`temp/`、`draft/`、隐藏路径和派生文件不会同步。首次 push 或大幅调整目录时，先让 Agent 汇总 compare 结果，尤其要核对 deletions。需要同步的正式文件不要只放在被排除的目录中。

建议按以下顺序协作：

1. 让 Agent 拉取最新 Workspace，在本地完成修改，并汇总准备 push 的内容。
2. 在浏览器审阅差异，针对当前 Workspace 运行 Full Verification。
3. 确认发布内容、消息和验证状态后，由你点击 Publish；也可以临时授予 `commit`，明确要求 Agent 发布。

要收回权限，可以在 `Settings` -> `Agents` 中：

- 将 `General permission` 调低或设回 `none`；这不会自动删除仍有效的逐题 grant。
- 在 `Authorized Problems` 中 `Revoke` 某一条 grant。同一问题可能有多条授权；要完全收回该题权限，还要确认没有其他有效 grant，并且 General permission 没有覆盖它。
- 点击 `Disconnect Agent` 删除整个会话及其请求和 grants，使原凭据失效。

## 多人协作与权限

每位用户都有独立的 Workspace。A 尚未 Publish 的修改，B 看不到。A Publish 后，B 会看到 Published Revision 已更新，需要先处理更新或冲突才能继续发布。

Problem 角色分为：

| 角色 | 主要能力 |
| --- | --- |
| `read` | 阅读题目；预览自己的 Workspace 或已有包；启动标准 Full Verification；查看和重新判可见验证；下载成功包 |
| `write` | 包含 read；编辑自己的 Workspace；使用 Custom Run；创建包；Publish |
| `owner` | 包含 write；通过 `Manage access` 管理直接 Problem 成员。Owner 身份固定，不能在普通角色表里转让 |

如果按钮只读、禁用或不存在，先检查当前角色。部分禁用控件会在悬停时显示原因。

Contest 同样有 read、write 和固定 owner。Contest membership 会把权限动态带到比赛中的题目：Contest read 对应 Problem read，Contest write/owner 对应 Problem write。这里不包含 Problem owner。管理 Problem 成员或把题加入比赛，仍需相应的直接管理权限。

Contest owner 可在比赛页右侧的 `Manage access` 授予或撤销 read/write membership。Problem 的直接成员仍在各题的 `Manage access` 中管理。

## 组一场 Contest

### 创建或导入

进入 `Contest` -> `My Contests`。`Create Contest` 用于创建空比赛。选择 `Import Polygon Contest Package` 可以上传 Polygon contest ZIP。随后在 `Review Contest Import` 检查比赛 slug、标题和每道题的新 slug，再确认导入。

### 编排题目

`Problems` 概览列出每题的 idx、时限、内存、题型、内容就绪情况，以及 Workspace、Verification 和 Package 状态。点击 `Manage problems` 可以搜索并加入题目、移除题目、修改 idx 和批量调整时限/内存。

Contest 只引用 Problem，不会复制题目内容。从比赛中移除一道题，也不会删除它的源码、历史或个人 Workspace。题目在页面和比赛包中的顺序都由 idx 决定。

### 整场审题

`Properties` 用于维护比赛属性及其多语言内容，`Statement Sources` 用于编辑比赛级 TeX 模板和资源。默认 banner、奇数题后插入空白页等选项也在这里。这些比赛级题面源不会改动各道 Problem 的题面。

比赛页右侧会在条件满足时显示整场 `Statements (HTML, <Language>)` 和 `Statements (PDF, <Language>)`：

- 选择 Workspace 来源时，系统只使用你现有的各题 Workspace。缺失的不会自动创建，也不会改用题目 owner 的 Workspace。
- 选择 Packages 来源时，每道题都必须已有可用 Native Package。预览不会自动构建包。
- 只有你对所有比赛题目都有 read 权限、所有题都具备所选来源，并且存在共同语言时，对应链接才会出现。

整场预览始终按 idx 顺序排列。某一道题渲染失败时，HTML review 会保留它的位置和诊断，其他成功题面仍可检查。

### 构建并下载比赛包

点击 `Build All Packages` 后，系统会为你有权构建且当前包未 `ready` 的题目排队。所有题的当前包都变为 `ready` 后，页面才显示 `Download Packages`。

下载时选择一种 External format。系统按 idx 组合每道题当前 Published Revision 对应的 Native Package。它不会自动补建缺失包，也不会回退到旧 revision。任何一道题缺包或 Adapter 失败，整场下载都会失败。

## 常见状态和处理方法

| 现象 | 这通常意味着什么 | 去哪里处理 |
| --- | --- | --- |
| 保存后其他人看不到修改 | 修改还在你的 Workspace | `Review workspace` -> `Publish new revision` |
| `Resolve Conflicts` 出现，Publish 不可用 | Published Revision 已由其他作者更新 | `Review Published Changes`，应用并复查合并结果 |
| Verification 显示 stale | 运行相关源码、配置或测试在验证后发生变化 | 对当前 Workspace 重新 `Start verification` |
| `Verification` 为 `failed`，但结果矩阵看不出原因 | 可能是生成、校验、main correct、编译、证据或 sanity 阶段失败 | 打开该次 Verification 的 Reason、Test Details 和 Diagnostics |
| `Packages` 显示没有 Published Revision | 当前题还没有正式版本 | 到 `Review workspace` 点击 `Publish new revision` |
| 发布新 revision 后，Package 显示 `stale`/`none` | 旧包属于旧 revision | 在 `Packages` 为当前 revision 创建新包 |
| Package 是 `not verified` | 当前 Native Package 没有与之匹配的共享 Full Verification 认证 | 有相应权限的作者可通过 `Verify` 或默认完整流程补齐认证 |
| Contest 没有整场题面预览链接 | 某题无权访问、缺所选来源，或各题没有共同语言 | 检查每题访问权、Workspace/Package 和语言 |
| Contest 不能 `Download Packages` | 并非所有当前包都 ready | 先 `Build All Packages`，查看失败题的 `Packages` |
| 找不到 `Connected Agents` | 当前界面名称不是这个 | 使用右上 `Settings` -> `Agents` |
| Registration URL 不存在、过期或已使用 | 注册 URL 只能注册一次，并且有效期较短 | 在 `Settings` -> `Agents` 重新点击 `Connect to Agent`，把新 URL 发给 Agent |
| approval URL 返回 404 或显示 expired | 当前登录账号与 Agent 所连接的账号不一致，或授权请求已过期、会话已不存在 | 先确认登录账号，再让 Agent 重新请求该题 Scope |
| Agent 报 `agent_general_permission_required` | 当前操作要求 General permission，逐题 grant 不能满足 | 到 `Settings` -> `Agents` 把该会话的 General permission 调到错误信息要求的 Scope |
| Agent 报 401 / `agent_credential_invalid` | 会话已断开、凭据已轮换，或 Agent 使用了旧的本地 state | 保留现有 state，并用新的 Registration URL 让 Agent 尝试重连、轮换凭据。原会话若已 `Disconnect`，再创建会话并重新授权 |
| Agent 选择高 Scope 后仍被拒绝 | 你的账号自身权限不足，或 grant 已过期/撤销 | 检查 `Manage access` / Contest membership 和 `Settings` -> `Agents` |

## 产品边界

- Polygon Replica 面向 ICPC 风格、整题 pass/fail 的出题流程，不支持按测试点累计的 partial scoring。
- 系统直接支持 `interactive` 和 `multi-pass`，但每种外部格式只接受其明确声明的子集。
- `Contest` 用于编排题目、审阅整场题面并下载比赛包。它不承载比赛当天的提交、判题榜单或现场管理。
- Polygon Replica 兼容已有 Polygon 来源和工作习惯，但不提供 `hosted Polygon private API` 的兼容替代。

需要核对更精确的格式、生命周期或权限边界时，可以继续阅读：

- [状态派生与生命周期](design/state-lifecycle.md)
- [访问模型](design/access.md)
- [题目源码协议](protocol/problem-source.md)
- [执行与验证协议](protocol/execution.md)
- [题面预览协议](protocol/statement-preview.md)
- [包导入与导出协议](protocol/package.md)

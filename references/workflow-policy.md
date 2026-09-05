# Coding Rules Guard 共享策略

只在需要判断模式、委派、证据等级或阶段转换时读取本文件。各阶段 Skill 不重复维护这里的规则。

阶段入口若缺少且确有必要的历史上下文，用户已授权时按[知识库查询](knowledge-lookup.md)只做一次有界 Query（Plan 优先，直接进入其他阶段则在该入口执行）；后续阶段复用路径、行号和结论。

## 模式

- `FAST`：只读或小范围低风险改动；无公共契约、宿主、发布、安全、迁移、新依赖或外部副作用。
- `FULL`：跨层、公共 API/协议、宿主、安装/发布、安全/权限、迁移、新依赖、并发/性能风险，或证据边界不明。
- 文件数量只是信号，不单独决定模式；单文件安全修改也必须 FULL，多文件机械改动可在风险明确时 FAST。

## 委派

只读任务不创建子代理，也不创建运行状态；其 Plan 只属于会话级审计，除非用户明确创建 run-state。有 `WRITE` 的任务（包括 FAST 和小改动）必须由当前会话主模型以 `session_main` 角色完成 Plan 并写入 `record-plan`，同时记录宿主显示的实际模型与推理强度，再由 `executor_default`（`gpt-5.6-luna + max`）执行。所有 `WRITE` 在验证结果确定后，也必须由当前会话主模型以 `session_main` 角色审核并写入 `record-review`，同时记录实际模型与推理强度；Plan 或 review 缺失、失败、主模型信息缺失或与当前任务 Diff 指纹不一致时不得继续。executor 是受保护配置角色；Plan 和审核不允许回退为固定 Sol/xhigh。路由器只能原样传递主会话 Plan；任何修改都必须重新由当前主模型规划并重新 `record-plan`。本地 CLI 只能校验可审计记录，不能密码学证明真实会话模型身份。

## 证据与结果

- 先运行一个能区分当前假设的最小真实入口；输入、实现或假设改变后才重复。
- 不编写故意错误的测试，不以修改断言、期望或 mock 让结果变绿。
- 哈希不是默认验证。只有安装副本、构建产物或发布包的内容一致性属于成功条件时才计算一次；输入文件、版本和提交未变化时复用本次运行的哈希证据，不在阶段之间重复计算。普通 FAST 或源码行为验证不做哈希校验。
- 证据等级：`source < test < browser < installed < host < production`。较低等级不能冒充较高等级。
- 结果：`pass`、`pass_with_gaps`、`blocked`、`fail`。`pass_with_gaps` 必须列出缺口，并由用户明确接受后才可正式交付。
- 至少记录成功路径和一个关键失败或边界路径；风险最高的相邻功能无法覆盖时必须进入 GAP。

## 严格最小化

- 新增测试必须同时有：已观察到的故障或已文档化的外部行为契约、现有真实入口或已有测试为何不能区分该行为的证据、以及它将阻止的一个明确回归。缺少任一项时，复用已有入口或不写测试；不得为覆盖率、私有实现细节、猜测性边界或“以防万一”新增测试、mock、夹具、测试辅助层或重复用例。
- 每个不同契约只保留一个最小、可区分的验证；同一输入和结论不得换名重复。格式、注释、纯文档或可由现有解析/构建直接验证的改动，不得再附加行为测试。
- 防御性代码必须对应已复现故障、已文档化宿主/协议契约，或明确的外部信任边界；在 Plan 中写明触发输入、预期可观察结果和最小处理范围。没有这些依据时，不得添加 catch-all、吞错、默认回退、盲目重试、无条件兼容分支、空值护栏或抽象层。
- 必要的防御不得掩盖失败：保留错误语义和可诊断信息，处理范围只覆盖已知触发条件；验证必须走该触发条件或等价的真实边界。

## 输出边界

- 不在业务仓库创建或更新报告、周报、状态文档、测试总结、变更总结或其他汇报型 Markdown；也不为了本次任务补充此类产物。唯一例外是本工作流规定的 Plan Markdown、任务运行状态和机器证据记录，或用户明确要求的某一份报告。
- 聊天只输出当前阶段要求的短字段或 Plan 文件链接；不得附加进度叙述、重复日志、完成总结或“供参考”的报告。交付阶段也只陈述必要的结果、阻塞项和用户要求的产物位置。

## 影响

写入前评估相关调用方、公共契约、数据和运行时副作用。输出只能使用：`no_known_impact`、`known_impact`、`unverified`，面向用户分别表述为“无已知影响”“已知影响”“影响未验证”。

## 机器信任边界

- 新运行必须在 `guard.py init --repo <仓库>` 记录 Git HEAD 和已有脏文件指纹。v4 状态还必须记录 planner profile；Plan 完成后使用 `guard.py record-plan` 写入 planner/model/reasoning、Plan revision 和绑定 `run_id`、repo、baseline HEAD、MODE、GOAL、WRITE、RISK、delivery flag 的 fingerprint。`plan → implement` 会重新校验配置、记录、revision 和 fingerprint；无记录、过期、篡改或配置漂移均阻断。`set-changes` 不接收 Agent 声明的文件列表，而是自动计算相对 baseline 的任务差异；未被任务触碰的既有脏文件不计入本任务，任务继续修改既有脏文件则会被识别。
- baseline 后 Git HEAD 改变时停止并要求新建状态，避免跨提交或切分支掩盖改动。
- `set-result pass_with_gaps` 只提出缺口，不授权。只有独立的 `authorize-gaps` 能写入机器时间、授权者和原因；Agent 不得把自己声明为用户或宿主。缺少授权记录不能 Complete 或 Deliver。

## 阶段

正常路径是 `当前会话主模型 Plan → Luna WRITE → Verify → 当前会话主模型审核 → complete`；只有需要 Git、安装、发布、版本核对或正式交付时走 `verify → deliver → complete`。Verify 确认实现缺陷后，先记录失败证据，再执行 `guard.py rework --reason <原因>`。第一次返回 Implement；同一 Plan 第二次连续返工返回 `REPLAN_RECOMMENDED` 警告；第三次返回 Plan、清除旧 `plan_record` 并设置 `REPLAN_REQUIRED`，此时普通 transition 不能重新进入 Implement，必须用 `guard.py revise-plan` 更新模式、目标、WRITE 和风险，然后重新 `record-plan`。修订 Plan 或成功完成 Verify 后 `rework_streak` 归零，累计 `rework_count` 不清零。旧 v1-v3 若停在 Plan 阶段不得直接进入 Implement，必须创建 v4 run-state。状态文件放在任务临时目录，不写入或暂存到业务仓库。

# Coding Rules Guard 共享策略

只在需要判断模式、委派、证据等级或阶段转换时读取本文件。各阶段 Skill 不重复维护这里的规则。

## 模式

- `FAST`：只读或小范围低风险改动；无公共契约、宿主、发布、安全、迁移、新依赖或外部副作用。
- `FULL`：跨层、公共 API/协议、宿主、安装/发布、安全/权限、迁移、新依赖、并发/性能风险，或证据边界不明。
- 文件数量只是信号，不单独决定模式；单文件安全修改也必须 FULL，多文件机械改动可在风险明确时 FAST。

## 委派

默认直接执行。只有工作能拆成互不写同一文件的独立子任务，并行能明显降低时间或提高独立审查质量时才委派。模型与推理强度按当前可用能力和任务难度选择；最高强度必须由代表性评测证明有收益。

## 证据与结果

- 先运行一个能区分当前假设的最小真实入口；输入、实现或假设改变后才重复。
- 不编写故意错误的测试，不以修改断言、期望或 mock 让结果变绿。
- 哈希不是默认验证。只有安装副本、构建产物或发布包的内容一致性属于成功条件时才计算一次；输入文件、版本和提交未变化时复用本次运行的哈希证据，不在阶段之间重复计算。普通 FAST 或源码行为验证不做哈希校验。
- 证据等级：`source < test < browser < installed < host < production`。较低等级不能冒充较高等级。
- 结果：`pass`、`pass_with_gaps`、`blocked`、`fail`。`pass_with_gaps` 必须列出缺口，并由用户明确接受后才可正式交付。
- 至少记录成功路径和一个关键失败或边界路径；风险最高的相邻功能无法覆盖时必须进入 GAP。

## 影响

写入前评估相关调用方、公共契约、数据和运行时副作用。输出只能使用：`no_known_impact`、`known_impact`、`unverified`，面向用户分别表述为“无已知影响”“已知影响”“影响未验证”。

## 机器信任边界

- 新运行必须在 `guard.py init --repo <仓库>` 记录 Git HEAD 和已有脏文件指纹。`set-changes` 不接收 Agent 声明的文件列表，而是自动计算相对 baseline 的任务差异；未被任务触碰的既有脏文件不计入本任务，任务继续修改既有脏文件则会被识别。
- baseline 后 Git HEAD 改变时停止并要求新建状态，避免跨提交或切分支掩盖改动。
- `set-result pass_with_gaps` 只提出缺口，不授权。只有独立的 `authorize-gaps` 能写入机器时间、授权者和原因；Agent 不得把自己声明为用户或宿主。缺少授权记录不能 Complete 或 Deliver。

## 阶段

正常路径是 `plan → implement → verify → complete`；只有需要 Git、安装、发布、版本核对或正式交付时走 `verify → deliver → complete`。Verify 确认实现缺陷后，先记录失败证据，再执行 `guard.py rework --reason <原因>`。第一次返回 Implement；同一 Plan 第二次连续返工返回 `REPLAN_RECOMMENDED` 警告；第三次返回 Plan 并设置 `REPLAN_REQUIRED`，此时普通 transition 不能重新进入 Implement，必须用 `guard.py revise-plan` 更新模式、目标、WRITE 和风险。修订 Plan 或成功完成 Verify 后 `rework_streak` 归零，累计 `rework_count` 不清零。状态文件放在任务临时目录，不写入或暂存到业务仓库。

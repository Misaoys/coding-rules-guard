---
name: coding-rules-verify
description: 阶段 3：执行不可绕过的真实验证，输出短证据卡片。
---

# 阶段 3：Verify

读取 [共享策略](../../references/workflow-policy.md) 和 Implement 提供的状态路径。严格沿 `Sol Plan → Luna WRITE → Verify → 当前会话主模型审核` 检查；验证尽量到达目标运行边界；退出码、固定 success、快照、mock、文件存在、静态检查或构建成功不能单独替代真实行为证据。

## 先证据，后修复

- 对每个待判断假设，先运行一次最小且能区分结果的真实入口或已有测试；得到结果后再选择修复、补覆盖或停止。只有输入、实现或假设发生变化时才重复运行，禁止为同一结论堆叠无新增信息的测试、脚本、夹具或快照。
- 不得为了演示“先红后绿”而先写明知错误、必失败或与已知契约不符的测试。优先复用已有测试和真实入口；新增测试仅在缺少覆盖、行为契约明确且它能防止已确认回归时才允许。
- 验证失败时，先判定失败来自实现、测试、环境还是前置数据；不能仅改断言、期望值或 mock 让结果变绿。确认实现缺陷后修复实现，再用同一最小入口验证一次；确认测试有误时，以既有契约或真实观察为依据修正该测试，而不是另写一份替代测试。
- 临时脚本、夹具、测试数据和日志在验证结束后清理或说明保留理由；不得混入交付。
- 新增测试必须逐项回查 Plan 中的行为契约、现有验证缺口和回归目标；缺失或无法通过真实行为证明其中任一项时，删除该测试而不是再补一层测试代码。
- 防御性代码必须通过其已声明的故障、宿主契约或外部边界验证；若只能假设风险而不能说明触发条件，删除该防御并在 `GAP` 如实记录未知边界。不得把测试数量或 catch-all 分支当作可靠性证据。

- 代码：走真实入口，检查输入→输出、状态/副作用和错误语义。
- UI/浏览器：实际交互；用户要求物理输入时使用真实鼠标/键盘。
- CEP/AE/原生：到达真实宿主、原生窗口或目标运行时。
- 安装/发布：加载、安装、启动/重启或产物检查，并核对当前版本。

至少记录成功路径和一个关键失败/边界路径。用 `guard.py record-evidence` 分别记录，随后用 `guard.py set-result` 写入 `pass / pass_with_gaps / blocked / fail`。缺少目标层证据时必须写明 GAP。`set-result` 只能申请 `pass_with_gaps`，不能同时批准；Agent 必须停下等待用户或宿主明确接受，再由独立的 `guard.py authorize-gaps --authorized-by user:<身份> --reason <原因>` 记录机器时间与授权。没有独立授权记录不得 Complete 或 Deliver。证据只写入运行状态；不得额外生成测试报告、验证报告或总结文档。

若影响评估指出调用方、相邻功能或公共契约可能受影响，覆盖风险最高的未受影响路径；无法覆盖时如实记录 GAP。确认实现缺陷时先记录失败证据并设置 `fail`，再执行 `guard.py rework --reason <原因>`。收到 `REPLAN_RECOMMENDED` 时重新检查假设；收到 `REPLAN_REQUIRED` 时停止返工并回到 Plan。命令会清除失效结果、证据和授权，修改后必须重新进入 Verify，不以先前通过代替复检。

所有有 WRITE 的任务在 `set-result` 后必须由当前会话主模型以 `session_main` 审核。需要正式交付时，先用显式路径暂存全部任务文件并确保任务路径不存在 index/worktree 分叉，再由当前会话主模型检查将要提交的 staged delta；无需交付时检查当前 worktree delta。随后执行 `guard.py record-review --profile session_main --model <CURRENT_SESSION_MAIN_MODEL> --reasoning-effort <CURRENT_SESSION_MAIN_REASONING> --result <pass|fail|blocked> --observed <结论>`。任何后续文件、index、证据、结果或返工变化都会使 review 失效；缺失、失败、主模型信息缺失或 Git delta 指纹过期的 review 会被机器门禁阻止。CLI 记录不能替代宿主级调用者身份认证，也不能密码学证明主会话模型身份。只读任务不创建子代理或 run-state，除非明确需要机器审计。

当前会话主模型审核通过并完成 `record-review` 后：无正式交付需求就 `transition --to complete`；需要 Git、安装、发布或正式交付就 `transition --to deliver`。`complete` 仅在所有门禁通过后删除绑定的 Plan Markdown；任何阻止完成的结果都必须保留该文档。只输出：`STATE / ENTRY / COMMAND / OBSERVED / EVIDENCE_LEVEL / RESULT / GAP / REVIEW`。

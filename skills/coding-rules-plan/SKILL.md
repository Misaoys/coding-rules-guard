---
name: coding-rules-plan
description: 阶段 1：选择 FAST/FULL，压缩范围、证据、模型和回滚为短 Plan 卡片。
---

# 阶段 1：Plan

只理解、判断和规划；不改业务文件、不提交、不创建子代理。先读取 [共享策略](../../references/workflow-policy.md)，由当前会话主模型以 `session_main` 完成初始 Plan，并记录宿主显示的实际模型与推理强度；信息不可得时阻断，不得回退为固定 Sol/xhigh。将恰好 7 个 Markdown 小节直接写入任务工作目录的 Plan Markdown。标题独占一行，字段之间空一行；不要输出总标题、完整日志或卡片外结语。

开始规划前，若项目历史、既有约定、已知修复或 runbook 可能改变判断，按[知识库查询](../../references/knowledge-lookup.md)执行一次有界 Query；简单自包含任务跳过，并复用命中路径、行号和必要结论。

```markdown
### MODE

`FAST` 或 `FULL`

### GOAL

- **成功条件：**...
- **非目标：**...

### WRITE

- **允许：**...
- **禁止：**...

### MODEL

- **主代理：**协调角色和所需推理强度
- **执行与审核：**只读为无；有 WRITE 时记录 `session_main`（当前会话主模型及其实际推理强度）规划、`executor_default`（Luna Max）执行和 `session_main` 审核；Implement 进入前必须用 `guard.py record-plan --profile session_main --model <CURRENT_SESSION_MAIN_MODEL> --reasoning-effort <CURRENT_SESSION_MAIN_REASONING>` 绑定本卡片

### VERIFY

- **真实入口：**...
- **成功路径：**...
- **失败/边界路径：**...
- **测试决策：**复用现有入口；或新增一个测试，写明行为契约、现有验证为何不足和要阻止的回归
- **防御决策：**无；或写明已知触发条件、可观察结果和最小处理范围

### ROLLBACK

...

### RISK

- **功能影响：**无已知影响；或已知影响：范围与行为；或影响未验证：原因
- **其他风险：**依赖、跨语言、宿主、性能、安全、Git 和未覆盖边界
```

将该卡片作为 Plan Markdown 的完整初始内容，通过 `guard.py write-plan --file <PLAN_FILE> --content-file <CARD_FILE>` 或 `--stdin` 直接写入。`PLAN_FILE` 必须是任务工作目录中的显式路径（例如 `work/plan.md`），不得自动写到业务仓库或暂存。聊天只返回该 Markdown 文件链接，不重复卡片内容，也不创建报告或总结文档。Implement 创建运行状态时必须把同一路径传给 `guard.py init --plan-file <PLAN_FILE>`；完成门禁通过后会自动删除它。7 个小节结束即停止；不得在 `RISK` 后追加执行说明、分支、委派或后续步骤；运行状态由 Implement 在真正写入前创建。

收到新需求时，必须复用同一 `PLAN_FILE`，先用 `guard.py prepend-requirement --file <PLAN_FILE> --content-file <REQUIREMENT_FILE>` 将需求原样添加到现有 Markdown 顶部；不得覆盖旧 Plan、另建脱节的第二份 Plan 或只在聊天中说明。新需求若改变 MODE、GOAL、WRITE、VERIFY 或 RISK，随后重新调用 planner，并按现有 replan/record-plan 门禁更新运行状态。

`VERIFY` 必须写明先运行的、能区分当前假设的最小真实入口或已有测试。先拿一次有效结果，再决定是否修复、补测试或停止；禁止为了制造“先红后绿”而编写明知错误的测试，也禁止并列堆砌对同一假设没有新增信息的测试。

测试或防御决策缺少具体契约与触发依据时，`WRITE` 必须明确为“不新增测试/防御代码”。不得以覆盖率、猜测的边界、私有实现、未来可能性或“更稳妥”为理由扩展范围；只有已复现故障、已文档化宿主/协议契约或明确外部信任边界可以成为新增最小防御的依据。

`RISK` 必须评估调用方、相邻功能、公共契约、数据和运行时副作用，并标注“无已知影响 / 已知影响：<范围与行为> / 影响未验证：<原因>”。只读任务不委派；所有有 WRITE 的任务由当前会话主模型以 `session_main` 规划、`executor_default`（`gpt-5.6-luna + max`）执行，并由当前会话主模型以 `session_main` 审核。Plan 与审核都必须记录宿主显示的当前模型和推理强度；信息不可得时阻断，不得回退为固定 Sol/xhigh。

若运行状态含 `REPLAN_REQUIRED`，本卡片必须重新判断原假设、MODE、GOAL、WRITE 和 VERIFY，而不是原样重放旧 Plan。路由器不得自行修改当前会话主模型 Plan；任何修改都必须重新由当前主模型规划。Plan 阶段仍不改状态文件；后续 Implement 用新卡片执行 `guard.py revise-plan`，再以 `session_main` 记录 Plan 后才能继续。只读任务默认只保留会话级审计，除非明确创建 run-state。

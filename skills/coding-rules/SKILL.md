---
name: coding-rules
description: 为编码任务选择 FAST/FULL 和当前阶段；只做路由，不执行修改、验证或交付。
---

# Coding Rules Guard 路由

只做分流，不编码、不验证、不提交。已知阶段时直接使用对应 skill，跳过本路由。需要判断模式或委派时读取 [共享策略](../../references/workflow-policy.md)。路由器只能原样传递 `$coding-rules-plan` 产出的当前会话主模型 Plan；如果需要改变 Plan，必须由当前会话主模型重新规划并重新记录，不得在路由层改写。

Plan 前若缺少会影响判断的历史约束、已知修复或 runbook 上下文，按[知识库查询](../../references/knowledge-lookup.md)只做一次有界 Query；简单自包含任务跳过，并随短卡片传递命中结论。

- 只读任务：会话级 `$coding-rules-plan`，完成后停止，不调用子代理群，也不创建 run-state；只有用户明确要求机器审计时才创建 run-state。
- 低风险写入：`$coding-rules-plan`（`session_main` 当前会话主模型规划）→ `$coding-rules-implement`（`executor_default` Luna max）→ `$coding-rules-verify`（`session_main` 当前会话主模型审核）；无 Git、安装、发布或正式交付需求时跳过 deliver。
- 跨层、高风险或正式交付：`$coding-rules-plan`（当前会话主模型）→ `$coding-rules-implement`（Luna max）→ `$coding-rules-verify`（当前会话主模型审核）→ `$coding-rules-deliver`。

只传递短卡片和运行状态路径，不复制背景、全文或完整日志。Plan 阶段不创建子代理；只有任务能安全拆成独立工作流且并行收益明确时，后续阶段才委派。executor 不可用或当前会话主模型信息不可记录时阻断并仅说明阻塞项，不静默 fallback。除 Plan Markdown、运行状态和机器证据外，不创建任何报告或总结文档。

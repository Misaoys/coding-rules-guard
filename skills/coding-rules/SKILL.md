---
name: coding-rules
description: 为编码任务选择 FAST/FULL 和当前阶段；只做路由，不执行修改、验证或交付。
---

# Coding Rules Guard 路由

只做分流，不编码、不验证、不提交。已知阶段时直接使用对应 skill，跳过本路由。需要判断模式或委派时读取 [共享策略](../../references/workflow-policy.md)。

- 只读任务：`$coding-rules-plan`，完成后停止，不调用子代理群。
- 低风险写入：`$coding-rules-plan` → `$coding-rules-implement` → `$coding-rules-verify`；无 Git、安装、发布或正式交付需求时跳过 deliver。
- 跨层、高风险或正式交付：`$coding-rules-plan` → `$coding-rules-implement` → `$coding-rules-verify` → `$coding-rules-deliver`。

只传递短卡片和运行状态路径，不复制背景、全文或完整日志。Plan 阶段不创建子代理；只有任务能安全拆成独立工作流且并行收益明确时，后续阶段才委派。

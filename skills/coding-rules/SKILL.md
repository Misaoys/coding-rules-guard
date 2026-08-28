---
name: coding-rules
description: 轻量路由：只选择当前阶段和 FAST/FULL 模式，不执行具体工作。
---

# Coding Rules Guard 路由

只做分流，不编码、不验证、不提交。已知阶段时直接调用对应 skill，跳过本路由。

- 只读任务：`$coding-rules-plan`，完成后停止，不调用子代理群。
- 有 `WRITE` 的低风险修改：`$coding-rules-plan` →（计划完成后默认调用子代理群）→ `$coding-rules-implement` → `$coding-rules-verify`；无 Git/发布/交付需求时跳过 deliver。
- 多文件、跨层、宿主、发布、安全或证据不明：`$coding-rules-plan` →（计划完成后默认调用子代理群）→ `$coding-rules-implement` → `$coding-rules-verify` → `$coding-rules-deliver`。

只传递短卡片，不复制背景、全文或完整日志。Plan 阶段只写 7 项卡片，不创建或调用子代理；卡片完成且 `WRITE` 非空后才默认启用子代理群。

---
name: coding-rules-implement
description: 阶段 2：按已完成的 Plan 和机器可校验的 WRITE 范围执行最小修改。
---

# 阶段 2：Implement

输入是 `$coding-rules-plan` 的 7 项卡片。缺卡片或范围不清就回到 Plan。执行前读取 [共享策略](../../references/workflow-policy.md)，从当前 Skill 目录解析 `../../scripts/guard.py`。

1. 在任务临时目录创建未跟踪的状态文件；不要把状态文件写入或暂存到业务仓库。新任务用 `guard.py init --repo <目标仓库>` 记录 Git baseline、`MODE / GOAL / WRITE / RISK`。若状态为 `REPLAN_REQUIRED`，先按新的 Plan 卡片执行 `guard.py revise-plan`；再用 `guard.py transition --to implement` 校验入口。
2. 只写 `WRITE` 范围；保留公共接口、协议和兼容行为。不要顺手重构或引入未计划的依赖、语言、权限或外部动作。
3. 所有有 WRITE 的任务（包括 FAST 和小改动）默认读取 `../../config/model-profiles.json`，由 `executor_default` 即 `gpt-5.6-luna + max` 执行；用户指定优先，不可用时按角色回退并报告。执行子代理必须服从相同 WRITE 范围，不得自行提交或扩范围。
4. 修复必须对应已观察到的真实结果；不编写故意失败或未经契约确认的测试，不重复制造无新增信息的诊断。
5. 修改后执行不带 `--file` 的 `guard.py set-changes`，让机器从 Git baseline 自动计算本任务实际文件，再用 `guard.py transition --to verify` 检查越界。前提变化、HEAD 改变或越界时停止并回到 Plan。

只输出：`STATE / CHANGED / UNTOUCHED / VERIFY_ENTRY`。不把编译、静态检查或文件存在写成真实功能通过。

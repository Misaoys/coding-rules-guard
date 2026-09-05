---
name: coding-rules-implement
description: 阶段 2：按已完成的 Plan 和机器可校验的 WRITE 范围执行最小修改。
---

# 阶段 2：Implement

输入是 `$coding-rules-plan` 的 7 项卡片。缺卡片或范围不清就回到 Plan。执行前读取 [共享策略](../../references/workflow-policy.md)，从当前 Skill 目录解析 `../../scripts/guard.py`。

1. 在任务临时目录创建未跟踪的状态文件；不要把状态文件写入或暂存到业务仓库。新任务用 `guard.py init --repo <目标仓库> --plan-file <PLAN_FILE>` 记录 Git baseline、`MODE / GOAL / WRITE / RISK`、Plan Markdown 路径和 `planner_profile`。`PLAN_FILE` 必须是当前会话主模型写入的同一个工作目录文件；完成门禁通过后由 CLI 删除。把当前会话主模型 Plan 原样交给 `guard.py record-plan --profile session_main --model <CURRENT_SESSION_MAIN_MODEL> --reasoning-effort <CURRENT_SESSION_MAIN_REASONING>`（记录模型、推理强度、revision 和绑定 Plan 文件路径的 fingerprint）后，再用 `guard.py transition --to implement` 校验入口；若状态为 `REPLAN_REQUIRED`，先按新的 Plan 卡片执行 `guard.py revise-plan`，再重新以 `session_main` 记录 Plan，旧记录不得复用。
2. 只写 `WRITE` 范围；保留公共接口、协议和兼容行为。不要顺手重构或引入未计划的依赖、语言、权限、外部动作、报告或总结文档。
3. 所有有 WRITE 的任务（包括 FAST 和小改动）默认读取 `../../config/model-profiles.json`，由 `executor_default` 即 `gpt-5.6-luna + max` 执行；executor 为受保护配置角色，配置不可用时阻断，不得静默回退。Plan 与审核都由当前会话主模型以 `session_main` 角色完成并记录实际模型与推理强度；信息不可得时阻断。执行子代理必须服从相同 WRITE 范围，不得自行提交或扩范围。
4. 修复必须对应已观察到的真实结果。只有 Plan 已写明行为契约、现有验证不足和要阻止的回归时，才新增一个最小测试；不得为覆盖率、私有实现、猜测性边界或“以防万一”编写测试、mock、夹具或测试辅助层。
5. 只有 Plan 已写明已复现故障、已文档化宿主/协议契约或明确外部信任边界时，才加入最小防御代码；实现必须限定在已知触发条件，保留错误语义和诊断信息。禁止 catch-all、吞错、静默默认回退、盲目重试、无条件兼容分支、空值护栏和未被契约需要的抽象。
6. 修改后执行不带 `--file` 的 `guard.py set-changes`，让机器从 Git baseline 自动计算本任务实际文件，再用 `guard.py transition --to verify` 检查越界。前提变化、HEAD 改变或越界时停止并回到 Plan。

只输出：`STATE / CHANGED / UNTOUCHED / VERIFY_ENTRY`。不把编译、静态检查或文件存在写成真实功能通过。

---
name: coding-rules-deliver
description: 阶段 4：仅在需要正式交付时审查 Diff、Git、版本和证据。
---

# 阶段 4：Deliver

仅当 Plan 要求 Git、发布、安装、版本核对或正式交付时执行。读取 [共享策略](../../references/workflow-policy.md) 和 Verify 状态；没有状态或未获准进入 Deliver 就停止。

1. 重新读取 branch、status、Diff、未跟踪和已暂存文件；先确认他人任务、worktree 和目标分支。
2. 当前会话主模型审核前已用显式路径暂存任务文件；用 `guard.py audit --repo <仓库>` 对照 init 时的 Git baseline、staged index、WRITE 与当前实际改动，拦截瞒报、index/worktree 分叉、越界文件和常见密钥；未被本任务触碰的既有脏文件保持在外。状态文件、日志和一次性诊断产物不得暂存。
3. 核对任务要求涉及的源码、构建产物、安装副本或发布版本。只有内容一致性属于成功条件时才计算一次哈希；输入、版本和提交未变化时复用同一运行证据，不跨阶段重复计算。Git 使用显式路径；无 Git 仓库或证据不足就如实报告。
4. 所有有 WRITE 的任务都必须已有当前会话主模型的 `session_main` 审核记录、结果为 pass、实际模型与推理强度非空且绑定当前任务 Diff 指纹；FAST 小任务也不例外。返工或任何后续改动后必须重新 Verify 和重新审核。
5. 审计通过后才能提交、合并或发布；Complete 会把最终 HEAD tree 与已审 staged delta 精确比对，出现额外提交路径、mode/blob变化或残留未审内容就阻止。通过后再执行 `guard.py transition --to complete`。外部写入仍以用户授权为边界。

只输出：`STATE / DIFF / GIT / VERSION / EVIDENCE / RISK`。RISK 明确写“无已知影响”“已知影响”或“影响未验证”，并列出尚未验证边界。不得创建或附加交付报告、变更总结或状态文档，除非用户明确要求。

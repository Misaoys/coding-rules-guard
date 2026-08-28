---
name: coding-rules-deliver
description: 阶段 4：仅在需要正式交付时审查 Diff、Git、版本和证据。
---

# 阶段 4：Deliver

仅当 Plan 要求 Git、发布、安装、版本核对或正式交付时执行；FAST 小改动默认不进入本阶段。

- 必须先有 Verify 证据卡片且 `RESULT` 通过；失败、未验证或版本不一致就停止。
- 有子代理组时，必须先有 SOL xhigh 最终审核；发生返工后还必须有 SOL xhigh 复工检验通过。没有完成这两道门，不得交付、提交或合并。
- 重新读取当前 branch、status、Diff、未跟踪/已暂存文件；排除无关改动、密钥、调试日志和旧快照覆盖。
- 核对源码、构建产物、安装副本、运行实例或发布版本；Git 使用显式路径和 git-agent-manager，先确认他人 worktree 和目标分支。
- 无 Git 仓库就如实报告，不伪造分支/提交；没有真实验证不得提交或合并。
- `RISK` 必须如实交付本次对其他功能的影响结论：明确写“无已知影响”或列出受影响功能、行为变化、已验证的路径与尚未验证的边界；禁止以未修改的文件数量替代影响评估。

只输出：`DIFF / GIT / VERSION / EVIDENCE / RISK`。

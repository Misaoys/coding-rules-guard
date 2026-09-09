# Coding Rules Guard 循环契约

仅在 Verify 失败、外部条件恢复、主动重规划或恢复上下文时读取本文件。成功路径只依赖共享策略和当前阶段 Skill。

## 路由

1. 记录当前 attempt 的真实 evidence／review 和结果。
2. 实现缺陷、验证契约问题或未知失败：写入当前诊断；能在原 Plan、WRITE 和验证条件内修复时调用 `rework`。
3. 需求、范围、验收或关键假设变化：调用 `revise-plan --reason`，递增 Plan revision，重新 `record-plan`；不要只换 hypothesis 文字。
4. 环境／外部输入阻塞：保留 blocked 结果，取得新的外部观察并在当前 attempt 记录环境诊断后调用 `retry-verify`。该命令不运行测试、不改代码、不写 pass。
5. 无新信息、预算耗尽或来源不可核对：停止自动继续，保留当前 state 和缺口，等待用户／宿主决定。

## 预算和历史

- `max_attempts` 默认6，允许1—20；它计算整个 run 的实现与 verify-only attempt 总数。
- `max_replans` 默认3，允许0—8；它计算成功的 `revise-plan` 次数。任何失败请求都不能增加计数。
- 已开始的最后一个 attempt 可以完成；只有创建下一个 attempt 才检查剩余预算。
- 每次关闭前先归档短 evidence、review、诊断、缺口、授权和指纹，再清空活动集合。历史记录永远不能满足新 attempt 的当前门禁。
- 第三次连续 `rework` 关闭旧 attempt 并回到 Plan，不提前创建下一轮；新的 Plan 必须重新记录。

## 诊断边界

诊断是模型声明的路由输入，不是根因证明、授权或通过记录。来源 ID 必须属于当前活动 attempt；来源快照、Plan revision、工作区指纹和结果变化会使诊断过期。相同失败可能返回 `NO_NEW_INFORMATION` 提醒，但全局预算和原有失败处置才是停止机制。

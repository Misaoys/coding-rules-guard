# Coding Rules Guard 验证登记契约

仅在 Plan 的 VERIFY 需要登记检查、或 Verify／Review／Deliver 要判断是否重复执行时读取。简单一次性人工检查不必为了登记而增加 JSON。

## 定义和执行

`record-plan --verification-spec` 只接收与当前 Plan VERIFY 对应的机器化子集：`check_id`、claim IDs、criterion digest、有序 argv/cwd/runner、输入路径／依赖覆盖、环境或外部状态身份和 repeat policy。定义摘要进入 Plan 指纹；Verify 阶段不能偷偷缩窄或替换定义。

`record-evidence --check-id <ID> --execution-record <JSON>` 登记真实 execution。执行来源分为 `host_receipt`、`adapter` 和 `agent_report`；后者保持自报性质。相同 execution ID 与完整绑定重复登记是幂等，不同采样必须使用不同 ID／sample ID。

## 查询语义

`check-verification` 是只读执行前查询，会读取必要的 Git／输入身份，但不运行测试、不调用模型、不写 state、不扣预算：

- `reuse`：当前 attempt、Plan、定义和完整输入绑定一致，且存在可信来源的当前通过 evidence；引用原 ID，不刷新时间。
- `run`：未登记、输入或定义变化、需要新样本、用户要求 force，或复用不适用。
- `diagnose`：同一输入下存在未解决 fail／blocked，先诊断，不机械刷绿。
- `unknown`：依赖覆盖、环境或执行来源不可充分核对；不能当作通过。
- `blocked`：phase、Plan、范围、格式或实时 Git 门禁不允许继续。

查询结果不是永久令牌。输入、代码、fixture、配置、环境、Plan、attempt 或权限变化后必须重新查询。Review／Deliver 仍各自执行语义、Git、授权和完整任务指纹门禁。

本地 CLI 没有宿主执行拦截 API；如果宿主只允许模型自行运行 shell，Skill 只能要求执行者消费查询结果，不能声称它必然跳过命令。未实现跨轮／跨任务缓存、函数级依赖图、图数据库或副作用命令自动重放。

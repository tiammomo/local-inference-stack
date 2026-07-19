# 推理与 Tool Use 增强路线图

状态：P0 第一批已实现并进入验收；有边界修复、多步/错误恢复与 traffic class 待实现。

审查基线：2026-07-19，Qwen3.5-9B Q5_K_M、128K 单 Slot、Q8_0 K/V、llama.cpp、
ModelPort Anthropic Messages 入口。

## 2026-07-19 第一批实现进度

- ModelPort strict response 已按工具编译并校验完整 JSON Schema，覆盖嵌套对象、数组、
  `required`、类型、`enum`、约束和 `additionalProperties`；外部 Schema 引用在入口
  fail-closed，错误值脱敏。
- Tool 请求日志已区分 `tool_called`、`continuation_tool_called`、`final_answer`、
  `answered_without_tool`、`completed_unobserved` 和错误终态，不再用 HTTP 2xx 代表模型
  一定调用了工具。
- 流式 `firstByteLatencyMs` 改为首个非空正文 delta 或 Tool Call 事件；非流式保持空值。
- 本项目新增 40 个固定闭环场景，5 个进入 standard 冒烟；Harness 校验工具选择和完整
  参数、执行确定性 Mock Tool、回传 `tool_result` 并验证最终答案。
- 运行台把请求可用性、协议通过、合法 Tool Call 与续轮完成分开，并使用独立 TTFT/E2E
  比例尺展示分位。

尚未完成：协议自动修复、多步与 `is_error` 恢复、Prompt Injection/超长结果、显式
traffic class、Reasoning/正文 Token 拆分和验证器驱动的档位升级。

## 目标

下一阶段不以单独提高 tok/s 为目标，而是提高以下可验证结果：

1. 模型在合适的任务上使用正确工具并生成符合 Schema 的参数。
2. 应用返回 `tool_result` 后，模型能够继续并给出正确最终答案。
3. 复杂推理在需要时获得更多预算，简单请求不承担不必要的思考延迟。
4. 长上下文、缓存和延迟指标可以真实反映用户体验，而不是只反映 HTTP 成功。
5. 所有增强都可在候选端口复现、回归和回滚。

## 2026-07-19 审查快照

最近 24 小时聚合报告中，当前本地 Provider 有 236 次请求：服务可用率 99.15%，
0 次超时，整体生命周期 P95 为 7.547 秒；92K--128K 档位 5 次请求的生命周期 P95
为 47.037 秒。运行时采样时解码约 93.7 tok/s，显存约 12.6/16.3GiB。

上述窗口包含本地真实上游验收流量。报告排除了临时 Mock Provider，但尚未为通过
`local_qwen` 发起的验收请求提供独立流量标签，因此这些数字用于发现问题和建立方向，
不作为业务 SLA 承诺。

Tool Use 有 91 次请求，表面成功率为 98.9%，其中 73 次是新字段上线前的
`unknown_legacy`，只有 18 次具有当前 `completed` 终态。当前 `completed` 表示包含
Tools 的 API 请求成功结束，不表示工具已经执行、结果已回传或最终任务正确完成。

固定质量集当前是 10 个 Case 重复三次，最近基线为 30/30；其中两个 Tool Use Case
均是强制单工具调用，只覆盖工具名和必填字段存在性，不构成闭环 Agent 能力评估。

## 改造前审查发现（保留为设计依据）

### 1. Tool 参数校验曾是结构级，不是 Schema 级

审查时 ModelPort 严格策略只校验：声明工具名、参数必须为 JSON Object、调用 ID、
调用数量、`tool_choice`、并行约束和 finish reason，当时尚未使用对应工具的
`input_schema` 校验 `required`、字段类型、`enum`、数组、嵌套对象和
`additionalProperties`。

llama.cpp 支持 Tool Calling，但其 Tool Schema 主要进入聊天模板，而不是自动成为
grammar 约束；`response_format` 的 JSON Schema 能力也不等同于 Tool Call 参数的
强约束。因此 ModelPort 必须在非流式返回或流式成功终态前做完整响应校验：

- [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [llama.cpp grammars](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)

### 2. Tool Use 成功率是请求级口径

审查时报告以请求 `status=success` 统计 Tool Use 成功，适合观察协议和服务可用性，
不适合命名为闭环工作流成功率；当前实现已经把“模型调用”和“任务完成”分开。

### 3. `firstByteLatencyMs` 曾不是真正统一的 TTFT

审查时流式路径的值接近上游响应建立时间，非流式路径使用完整请求耗时，不能混合
解释为首 Token 延迟；当前实现已改为 stream-only 首语义 TTFT，非流式保持空值。

### 4. 逻辑 Profile 与验收流量需要显式化

审查窗口中 137 次请求使用 `local_qwen:qwen3.5-9b-q5km`，另有 2 次使用旧的
`local_llamacpp` 物理路由；只有 97 次使用 `qwen3.5-fast/code/deep`。这可能是验收
调用，也可能是应用绕过逻辑档位。两种情况都需要治理：测试流量应显式标记，业务调用
应优先使用逻辑模型并记录实际应用的 reasoning/sampling profile。

## P0：Tool Use Reliability v2

实现位置以 ModelPort 为主，本项目负责真实上游验收、发布门禁和运行台。

### P0.1 完整 JSON Schema 校验

- 在请求入口校验并编译受支持的 JSON Schema 子集。
- 将 Tool 名映射到对应 Schema，对模型生成的每个参数对象逐一校验。
- 至少支持 `required`、`type`、`enum`、`const`、数组、嵌套对象和
  `additionalProperties`。
- 不支持或语义不明确的关键字在请求入口 fail-closed，不能静默忽略。
- 只持久化错误类别和计数，不保存 Schema、工具名或参数正文。

### P0.2 一次有边界的协议修复

- 仅在模型输出尚未交给客户端、工具不可能已经执行时允许一次修复。
- 修复提示只包含确定性的 Schema 错误路径，不附加业务 Prompt。
- 非流式响应可以直接缓冲；流式 Tool Block 必须先完成聚合和校验，再决定交付或修复。
- 不对超时、取消、权限错误或工具执行失败做隐式模型重试。
- 记录 `repair_attempted`、`repair_reason` 和 `repair_recovered` 聚合指标。

### P0.3 Tool 工作流状态模型

在不保存内容的前提下记录以下状态：

```text
tools_offered
  -> model_called_tool | model_answered_text | model_refused
  -> schema_valid | schema_invalid
  -> tool_result_received | abandoned | expired
  -> continued
  -> final_answer_completed | final_answer_missing | max_tokens
```

建议同时记录：工具数量、`tool_choice` 模式、调用数量、步骤数、并行与否、是否
`is_error`、修复次数、Reasoning Token、最终正文 Token 和各阶段延迟。全部字段必须
是枚举或数值，不得保存正文与参数。

### P0.4 闭环质量集

把 Tool Use 从当前两个强制单工具 Case 扩展到 40--60 个可自动判定场景：

- 多个相似工具中的正确选择、无需工具时直接回答、required/auto/named/none。
- 缺字段、错误类型、enum、额外字段、嵌套对象、数组、Unicode 和大 Schema。
- SSE 参数分片、重复 ID、错误 finish reason、并行禁止和多个结果一次回传。
- 两至四步调用、`is_error=true` 后修正参数、Tool Result 后产生最终正文。
- Tool Result 中的指令注入、超长结果、不可重试错误和客户端放弃。

Mock Tool Executor 只属于测试夹具，不进入 ModelPort 生产网关。业务工具仍由应用执行。
Anthropic 的标准客户端工具循环同样要求应用执行工具并回传 `tool_result`：
[How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)。

## P1：推理增强

### P1.1 验证器驱动的自适应升级

推荐默认路径：

```text
qwen3.5-fast
  -> 结构校验、规则或轻量测试失败
qwen3.5-code
  -> 编译、测试、Lint 或 Tool 执行反馈仍失败
qwen3.5-deep
```

- 确定性短任务可以显式关闭 Thinking。
- 日常编码和 Tool Use 使用 `qwen3.5-code`。
- 只有复杂规划、跨文件调试或验证失败后才升级 `deep`。
- 最多升级两次；一旦已发生外部副作用，不自动重放工具。
- 记录首轮成功率、升级率、最终成功率、总 Token 和总耗时。

### P1.2 使用外部验证替代无界思考

代码任务优先把编译器、类型检查、测试、Lint 和受限运行结果作为下一轮输入。结构化
输出使用 JSON Schema；检索任务使用可验证引用；计算任务使用 Calculator。只有缺少
确定性验证器时才依赖第二次模型自评。

### P1.3 上下文工作集管理

128K 保持为生产容量，不等于每次都填满。建议：

- 常规任务目标小于 32K。
- 48K--64K 开始生成带来源句柄的阶段摘要。
- 原始历史放入可检索存储，按当前任务取回相关片段。
- System Prompt、工具数组和固定项目规则保持稳定顺序，提高前缀缓存复用。
- Tool Registry 较大时先选出约 5--12 个相关工具，不把全部工具注入每一轮。

Qwen3.5 官方建议 Thinking 场景至少保留 128K，并给出了当前 fast/code/deep 所使用的
通用思考与精确编码采样参数；同时建议多轮历史不回放 Thinking 正文：
[Qwen3.5-9B README](https://huggingface.co/Qwen/Qwen3.5-9B/blob/main/README.md)。

### P1.4 精确 Token Count 凭证

当前 Anthropic 推理在准入阶段会再次执行精确 Count Tokens。可增加短时签名凭证，
绑定规范化请求摘要、Provider、模型、Token 数、策略版本和过期时间。客户端刚调用过
`/count_tokens` 时可以复用凭证；请求变化、凭证过期或校验失败时仍执行精确计数。
服务只保留摘要和数值，不缓存 Prompt。

### P1.5 真实 TTFT 与思考完成度

- 流式路径记录第一个可交付语义事件或 Token 的时间。
- 非流式只报告完整响应延迟，不伪装成 TTFT。
- 单独记录 prompt、queue、reasoning、final decode 和 tool round-trip 阶段。
- 将 `max_tokens`、有 Reasoning 无正文、空正文和客户端取消分开。

## P2：候选实验

### Q6_K 权重

当前生产约剩 3.4GiB 显存，可在 `18081` 候选端口测试 Q6_K 权重。它必须通过相同的
128K/Q8 KV、118K 召回、92K 解码、Reasoning、Tool Use 和三次质量集，并保留足够的
峰值显存余量。没有质量提升证据时继续使用 Q5_K_M。

当前 Q8_0 指的是 KV Cache。将模型权重也切换到 Q8_0 会显著减少 16GB 单卡余量，
不作为默认候选。

### 引擎、MTP 与原生构建

- llama.cpp 升级一次只跨一个固定版本，并走串行候选发布。
- MTP 仅在新版本修复已知长上下文回退后复测，不直接恢复生产。
- 原生 `sm_120` 构建只有在相同质量下显著改善冷/热 TTFT 或吞吐时才晋级。

## 指标名称与验收门槛

| 指标 | 定义 | 第一阶段门槛 |
| --- | --- | --- |
| Request availability | 排除客户端取消后的服务成功率 | 不低于当前基线 |
| Tool protocol pass | Tool 请求通过协议转换和帧校验 | `>=99.5%` 合成集 |
| Tool schema pass | 已生成调用满足对应 JSON Schema | 不允许违规调用获得可执行成功终态 |
| Tool workflow completion | 收到 Tool Result 后产生合法最终终态 | `>=95%` 固定闭环集 |
| Tool task success | Mock Tool 实际执行后最终答案正确 | `>=95%`，每 Case 三次 |
| Repair recovery | 首次协议错误经一次修复后恢复 | 观测项，不以增加重试换取虚假成功 |
| Final answer completion | Thinking 请求存在非空最终正文 | 必须单独统计并建立基线 |
| True TTFT | 流式首个可交付语义 Token | 按上下文档位和逻辑模型比较 |

门槛只约束固定合成集和同版本 A/B，不把少量真实请求直接解释为模型能力。新功能必须
先建立未优化基线，再比较成功率、延迟、Token 和显存；任何质量下降都阻止晋级。

## 分层职责

| 层次 | 负责增强 | 不负责 |
| --- | --- | --- |
| `local-inference-stack` | Runtime、量化/引擎 A/B、质量集、候选发布、Dashboard | 认证、业务工具执行 |
| ModelPort | 协议、Schema 校验、修复策略、工作流观测、Token 准入、逻辑 Profile | 任意业务工具代码 |
| 应用 / Agent | Tool Registry、选择、执行、审批、沙箱、幂等、业务最终判定 | 修改底层推理容量 |

工具应按只读、写入、破坏性操作分级；有副作用的工具必须由应用执行权限确认、幂等键、
超时和输出大小限制。ModelPort 不演进为任意 Tool Executor。

## 推荐实施顺序

1. 修正指标名称和测试流量标签，建立可信基线。
2. ModelPort 实现 Schema 完整校验和 Tool 工作流状态。
3. 增加一次有边界的修复，并完成 40--60 Case 闭环验收。
4. 运行台展示协议、Schema、Workflow、Task 四种成功率。
5. 实现验证器驱动的 fast/code/deep 自适应升级。
6. 实施上下文压缩、工具预选和 Token Count 凭证。
7. 最后评估 Q6_K、llama.cpp 新版本、MTP 和原生 CUDA 构建。

## 暂不采用

- 不把生产上下文降到 64K；吞吐 Profile 除外。
- 不恢复 q4 KV、非精确 cache reuse 或未经复验的 MTP。
- 不把全部 Tool Registry 固定注入每个请求。
- 不用 HTTP 2xx 代表 Tool 任务成功。
- 不让 ModelPort 自动执行任意业务工具或绕过应用授权。

# ModelPort 接入

## 接入契约

机器可读契约为
[`contracts/local-qwen-provider-v1.json`](../contracts/local-qwen-provider-v1.json)。
契约版本变化必须同时通过 `scripts/acceptance-suite.sh standard`，不能只修改一侧配置。

ModelPort 使用部署级 provider `local_qwen` 表达业务身份，llama.cpp 只是
`infra/local-inference-stack` 可独立替换的推理引擎实现。服务端使用以下稳定标识：

```text
Runtime endpoint: http://qwen-runtime:8080/v1
Upstream model ID: qwen3.5-9b-q5km
ModelPort provider ID: local_qwen
ModelPort 显式路由: local_qwen:qwen3.5-9b-q5km
```

ModelPort 与运行时通过 `MODELPORT_NETWORK_NAME` 指定的外部 Docker 网络通信，
默认是 `modelport_default`。`qwen-runtime` 是稳定 DNS alias；服务名、容器名、
镜像和模型文件路径不是跨项目契约。宿主机端口不参与容器间请求。

职责边界：

| 配置 | 所属项目 | 说明 |
| --- | --- | --- |
| 权重、量化、上下文、最大生成量、引擎参数 | `infra/local-inference-stack` | 运行时容量与行为 |
| `qwen-runtime`、`qwen3.5-9b-q5km`、OpenAI usage 字段 | 跨项目契约 | 端点、模型身份和 Token 交接 |
| `local_qwen`、展示名、路由、费率和历史用量 | ModelPort | 网关策略与账务口径 |

Token 数以运行时响应中的 `prompt_tokens` / `completion_tokens` 为准；ModelPort
只在上游未返回 usage 时做本地估算，不在两个项目中维护重复 tokenizer。当前
采用内部费率卡 `local-qwen-2026q3-v1`，由 ModelPort 独立配置并对每次请求保存
价格快照，`infra/local-inference-stack` 不复制可执行计费配置：

| Token 类型 | USD / 1M tokens |
| --- | ---: |
| 输入 | 0.05 |
| 输出（包含 reasoning） | 1.50 |
| Cache Write | 0.05 |
| Cache Read | 0.01 |

费率按本机单 Slot 实测的预填充和解码吞吐，结合硬件、电力、维护及利用率成本
制定；不套用云端 Qwen 价格，不设置单次请求最低消费。三个逻辑模型档位解析到
同一上游模型，因此使用同一费率。运行时只负责准确返回 usage；后续调价只修改
ModelPort，并以新的费率卡版本记录，不追溯改写历史费用。

协议分层采用“运行时 OpenAI、应用侧 Anthropic”：llama.cpp 原生且稳定地表达
`reasoning_content`、请求级思考开关和预算；ModelPort 继续给 Claude Code 与
Anthropic SDK 暴露 Messages API。这样既不要求 llama.cpp 模拟 Anthropic，也不
要求现有应用改协议。

Tool Use 同样在 ModelPort 做协议语义：本地 Provider 先聚合流式函数参数，再用
严格响应策略核验声明工具名、对象 JSON、调用 ID、`tool_choice`、并行数量、finish
reason 和每个工具的完整 `input_schema`。非流式违规响应直接失败；流式参数 delta
可能先到达客户端，但违规调用不会收到成功的 `content_block_stop`，客户端必须等到
该终态后才允许执行工具。这不会改变 llama.cpp 原生 OpenAI 端点，也不会把本地模型
规则写入通用 adapter。一次有边界的缓冲修复仍属于下一阶段，详见
[`ENHANCEMENT_ROADMAP.md`](ENHANCEMENT_ROADMAP.md)。

推理服务默认开启思考。ModelPort 将 Anthropic `thinking` 映射为 llama.cpp 的
`chat_template_kwargs.enable_thinking` 和 `thinking_budget_tokens`。应用应把渲染后
输入控制在约 92K，并允许最多 32,768 输出 tokens，不要把 128K 容量全部分配给输入。

逻辑模型档位均解析到同一个上游模型，不增加显存：

| ModelPort model | 默认思考预算 | 默认采样 | 用途 |
| --- | ---: | --- | --- |
| `qwen3.5-fast` | 512 | 通用思考 | 短问答、分类、简单工具选择 |
| `qwen3.5-code` | 4,096 | temperature 0.6、presence 0 | 日常编码和 Agent，推荐默认 |
| `qwen3.5-deep` | 16,384 | 通用思考 | 复杂调试与架构推理 |

客户端显式 `thinking.budget_tokens` 优先于档位默认值；显式
`thinking.type="disabled"` 会关闭该请求的思考。客户端显式采样值也优先于档位
默认；未匹配逻辑别名的模型保持运行时默认值。

ModelPort 还为 `local_qwen` 显式启用了精确 Token 计数：

```text
POST http://127.0.0.1:38082/v1/messages/count_tokens
```

该路径把 `qwen3.5-fast/code/deep` 别名解析为真实上游模型，再使用 llama.cpp 的
Qwen tokenizer 与当前聊天模板计数；不会使用“字符数除以 4”的计费近似，也不会
fallback 到其他 Provider。应用应在拼装 system、工具 schema、历史消息和当前问题
后调用它，以 `input_tokens + max_tokens <= 131072` 为硬容量条件。开启思考时仍建议
把输入目标控制在 94,208 tokens 以内，为思考、正文和模板波动保留余量。ModelPort
会在获取推理 Slot 和创建计费租约前执行同样的精确准入：超过硬容量或思考建议上限
返回可操作的 400，绝不静默截断；显式关闭思考时只应用 131,072 硬上限。

请求账本提供请求级聚合 `toolOutcome`：`tool_called` 表示模型产生了通过严格校验的
调用，`continuation_tool_called` 表示 Tool Result 后继续调用，`final_answer` 表示
续轮产生最终回答，`answered_without_tool` 表示首轮未调用直接回答；另有未观测完成、
客户端取消、超时、协议错误、上游/交付错误和历史未知。它不保存工具名、参数、结果和
原始响应；运行台只消费这些枚举和计数。业务工具是否正确执行以及最终任务是否正确，
仍由应用或本项目闭环 Harness 判定。

`local_qwen` 的 strict response 会按每个工具的完整 `input_schema` 校验模型参数；
流式日志的 `firstByteLatencyMs` 从上游尝试开始，停止于首个非空正文 delta 或 Tool
Call 事件，非流式请求不填该字段。

逻辑模型是应用的稳定入口。显式物理路由
`local_qwen:qwen3.5-9b-q5km` 保留用于诊断和验收，不应成为业务默认；否则不能明确
表达 fast/code/deep 的思考与采样意图。后续日志需要记录实际命中的 Profile，并给
本项目验收请求增加独立 traffic class，避免污染生产趋势。

## 客户端配置

Anthropic SDK 或 Claude Code 使用：

```env
ANTHROPIC_BASE_URL=http://127.0.0.1:38082
ANTHROPIC_AUTH_TOKEN=<ModelPort 的 MODELPORT_AUTH_TOKEN>
ANTHROPIC_MODEL=qwen3.5-code
ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.5-deep
ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3.5-code
ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.5-fast
```

## 直接调用 ModelPort

```bash
curl --noproxy '*' http://127.0.0.1:38082/v1/messages \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-code",
    "max_tokens": 8192,
    "thinking": {"type": "enabled", "budget_tokens": 4096},
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

精确计数示例：

```bash
curl --noproxy '*' http://127.0.0.1:38082/v1/messages/count_tokens \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-code",
    "messages": [{"role": "user", "content": "你好，world"}]
  }'
```

## 协议验收

需要分别验证：

1. `/v1/models` 能发现 `qwen3.5-9b-q5km`。
2. 非流式消息能返回 Anthropic content blocks。
3. 流式消息包含合法 SSE 终止事件。
4. `max_tokens` 能正确映射到 llama.cpp。
5. Tool Use 的单工具、参数 JSON 和流式参数能够通过。
6. `scripts/modelport-context-acceptance.sh` 能在默认思考模式下经 Anthropic
   Messages 完成约 92K 中部召回，且正文必须精确等于验收码。
7. 三个逻辑档位、显式预算和 `thinking.type="disabled"` 均按预期映射。
8. `scripts/modelport-token-count-smoke.sh` 的直连与 ModelPort 精确计数一致。
9. 严格 Tool Use 响应校验拒绝未声明工具、非法参数和并行/choice 违约；正常的
   非流式、流式、`is_error` 及 tool-result continuation 全部通过。
10. `scripts/modelport-context-admission-smoke.sh` 验证硬超限请求被拒绝且不静默截断。

当前第 9 项中的“非法参数”指非法 JSON 或非 Object。完整 Schema 违约、多工具选择、
多步执行和最终任务正确性属于下一阶段的闭环门禁，不能用当前通过结果代替。

只有在上述测试发现可复用的协议问题时才修改 ModelPort adapter。协议转换仍应
保持 OpenAI-compatible 通用；具体模型名称、展示和费率只进入部署配置。

本次真实上游验收发现，Reasoning 模型可能在默认 128 token 内尚未
生成 tool result 后的正文。因此 ModelPort 的通用 Tool Use 验收脚本增加了
`--max-tokens N` 及 `MODELPORT_TOOL_USE_MAX_TOKENS`；这只改变测试请求预算，
不改变网关限制，也不绑定 Qwen。

流式工具参数可能分散在多个 `input_json_delta` 事件中。验收脚本会拼接全部
`partial_json` 后再解析，避免把合法的上游分片误判为 Tool Use 不兼容。

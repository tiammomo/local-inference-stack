# API 使用

已部署 runtime 在宿主机只监听 `http://127.0.0.1:18080`。它没有认证，只适合本机诊断和
可信单用户应用；不要把端口转发到局域网或公网。长期应用接入、认证、逻辑模型和 Tool Use
适配应使用 [ModelPort](MODELPORT.md)。

## 健康与模型身份

```bash
curl --noproxy '*' --fail http://127.0.0.1:18080/health
curl --noproxy '*' --fail http://127.0.0.1:18080/v1/models
```

健康响应应包含 `{"status":"ok"}`。请求中的 `model` 必须使用当前 Catalog 的
`servedModelId`；已验证部署为 `qwen3.5-9b-q5km`。不要依赖容器文件名或上游仓库名作为 API ID。

## Chat Completions

```bash
curl --noproxy '*' --fail http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "model": "qwen3.5-9b-q5km",
  "messages": [{"role": "user", "content": "只回复：连接成功"}],
  "max_tokens": 512,
  "temperature": 0
}
JSON
```

最终文本位于 `choices[0].message.content`，Token 统计位于 `usage`。服务固定了经过验收的默认
采样参数；调用方可以显式覆盖请求级参数，但改变采样、输出上限或 Prompt 仍需由应用自行评估质量。

## 思考输出

```bash
curl --noproxy '*' --fail http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "model": "qwen3.5-9b-q5km",
  "messages": [{"role": "user", "content": "计算 17+25，最终答案只回复数字。"}],
  "max_tokens": 512,
  "temperature": 0,
  "chat_template_kwargs": {"enable_thinking": true}
}
JSON
```

当前 llama.cpp Profile 使用分离的 `reasoning_content` 和最终 `content`。应用不应把思考文本
当作最终答案、审计结论或可安全展示内容；对外行为仍应依据最终结果与独立校验。

## 流式响应

在相同请求中加入 `"stream": true` 即可获得 Server-Sent Events。客户端必须逐条处理
`data:` 事件、容忍 reasoning 与 content 分块，并在 `[DONE]` 后结束；不要假设一个 JSON chunk
就是完整消息。非流式模式更适合验收、严格 Tool 参数校验和可复现调试。

## Token 与上下文准入

已验证 latency Profile 是单 Slot、131,072 tokens，推荐 reasoning 输入不超过 94,208，单次输出
最多 32,768。模板、系统消息、工具 schema、历史消息、输入和输出预算共同占用上下文；服务关闭了
context shift，不应依赖静默截断。精确 Anthropic 风格计数入口为：

```bash
curl --noproxy '*' --fail http://127.0.0.1:18080/v1/messages/count_tokens \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-9b-q5km","messages":[{"role":"user","content":"你好"}]}'
```

应用侧需要逻辑模型工作集、队列和 fail-closed Token 门禁时，应使用 ModelPort；本接口只暴露单个
物理模型和单个 latency Slot。

## 错误与诊断

- 连接拒绝：先运行 `./stack status --json` 和 `./scripts/runtime.sh logs`。
- `/health` 非 `ok`：不要重复 deploy；检查 Docker、事务和 canonical Profile。
- HTTP 4xx：检查模型 ID、JSON、上下文预算和请求字段。
- HTTP 5xx 或请求超时：保留合成请求的时间和状态码，避免把 Prompt/回复或凭据写入公开 issue。
- shell 设置了代理时继续使用 `--noproxy '*'`，避免 loopback 请求被转给代理。

运行状态和恢复步骤见[运维与恢复](OPERATIONS.md)，协议边界见[架构与边界](ARCHITECTURE.md)。

# ModelPort 接入

ModelPort 是可选的应用网关；独立 llama.cpp 部署不依赖它。跨仓库能力以
[`local-qwen-provider-v1.json`](../contracts/local-qwen-provider-v1.json) 为准。

## 稳定标识

```text
容器网络端点  http://qwen-runtime:8080/v1
上游模型      qwen3.5-9b-q5km
Provider      local_qwen
宿主机网关    http://127.0.0.1:38082
```

容器通过外部 Docker 网络 `modelport_default` 通信；宿主机 `18080` 不参与内部路由。

| 逻辑模型 | 默认思考 | 典型用途 |
| --- | ---: | --- |
| `qwen3.5-fast` | 关闭/轻量 | 短问答、分类、简单工具选择 |
| `qwen3.5-code` | 4K | 日常编码与 Agent，推荐默认 |
| `qwen3.5-deep` | 16K | 复杂调试和架构推理 |

三个逻辑模型共享同一权重。客户端显式思考预算和采样参数优先。ModelPort 在请求进入
推理 Slot 前执行精确 Token 计数，并按机器契约中的硬上限和各档工作集上限
fail-closed，不静默截断。

Tool Use 的协议转换和 Schema 校验属于 ModelPort；真实工具仍由应用执行。

## 客户端

```env
ANTHROPIC_BASE_URL=http://127.0.0.1:38082
ANTHROPIC_AUTH_TOKEN=<ModelPort token>
ANTHROPIC_MODEL=qwen3.5-code
ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.5-deep
ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3.5-code
ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.5-fast
```

```bash
curl --noproxy '*' http://127.0.0.1:38082/v1/messages \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-code","max_tokens":8192,"messages":[{"role":"user","content":"你好"}]}'
```

精确计数入口为 `POST /v1/messages/count_tokens`。

## 兼容与验收

```bash
python3 scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR"

MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh standard

# 正式联合发布：额外要求两个仓库状态和固定 revision 匹配
python3 scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR" --release
```

Provider、模型映射、思考/采样、Token 限制或 Tool Use 契约变更时，必须协调两个仓库
并重新运行 `standard`。

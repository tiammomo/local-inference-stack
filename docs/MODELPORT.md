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

| 逻辑模型 | 默认思考 | 推荐输入 | 最大输出 | 典型用途 |
| --- | ---: | ---: | ---: | --- |
| `qwen3.5-fast` | 关闭/512 | 24,576 | 4,096 | 短问答、分类、简单工具选择 |
| `qwen3.5-code` | 4,096 | 57,344 | 16,384 | 日常编码与 Agent，推荐默认 |
| `qwen3.5-deep` | 16,384 | 94,208 | 32,768 | 复杂调试和架构推理 |

三个逻辑模型共享同一权重。客户端显式思考预算和采样参数优先。ModelPort 在请求进入
推理 Slot 前执行精确 Token 计数，并按机器契约中的硬上限和各档工作集上限
fail-closed，不静默截断。

Tool Use 的协议转换和 Schema 校验属于 ModelPort；真实工具仍由应用执行。

## 40 人团队第一阶段边界

第一阶段仍只有一个 ModelPort 实例和一个本地 GPU 推理节点。本仓库继续只负责该节点
的权重、运行参数、健康、容量和主机验收，不实现用户身份、项目策略、云端预算、公平
队列或工具执行；这些控制均由 ModelPort 实现并持久化。

ModelPort 是唯一面向用户的入口，并可按数据策略接入审核通过的云 Provider。
本仓库不会自动发现或自动信任新的 GPU 节点；增加节点时必须逐台注册并重新生成与主机、
模型、镜像和配置绑定的验收凭证。单个 GPU 节点故障时，`local_strict` 流量不可用，
只有明确允许云端的模式才能由 ModelPort 降级。

当前单实例部署不能宣称网关高可用。第二个 ModelPort 实例需要先解决共享 Session、限流、
Provider 健康和控制状态，再单独完成故障切换验收。

当前联合契约固定以下入口：默认 `local_strict`；客户端仅可用
`x-modelport-hybrid-mode` 将项目策略进一步收紧；`unknown`/`sensitive` 分类永远不外发。
本地每用户最多 1 个执行、2 个排队，全局交互队列 16；`local_first`/`balanced` 预计等待
超过 5 秒时才可溢出到项目批准的云端，`local_strict` 最多等待 60 秒后返回带
`Retry-After` 的 429。`batch` 使用独立低优先级队列。这些值由双仓库兼容检查共同锁定。

## 5 分钟静态接入

ModelPort 仓库提供与本契约同步维护的完整示例：

```text
deploy/local-inference/modelport.local-qwen.toml
```

全新配置可复制为 `config.toml`；已有 Provider 时应合并，不能直接覆盖。两个仓库
不要求相邻放置，始终显式设置路径：

```bash
export MODELPORT_PROJECT_DIR=/path/to/ModelPort
export LOCAL_INFERENCE_STACK_DIR=/path/to/local-inference-stack

cd "$MODELPORT_PROJECT_DIR"
./scripts/local-inference-check.sh \
  --stack-dir "$LOCAL_INFERENCE_STACK_DIR"
```

这个联合检查只读取两个仓库的契约与配置，不请求模型、不启动容器、不改变 GPU。
也可以从本仓库运行等价命令：

```bash
python3 scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR"
```

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
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm install
nvm use
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh standard

# 正式联合发布：额外要求两个仓库状态和固定 revision 匹配
python3 scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR" --release
```

Provider、模型映射、思考/采样、Token 限制或 Tool Use 契约变更时，必须协调两个仓库
并重新运行 `standard`。该层固定 Linux Node.js 24，还要求 ModelPort runtime、当前调用凭据、
aggregate snapshots 和 loopback Dashboard 在模型测试前通过 preflight；完整准备步骤见
[验收与发布](ACCEPTANCE.md#standardfull-联合前置条件)。

## 聚合运维契约

Dashboard 已采用零凭据快照架构：短生命周期 Collector 写入
`contracts/operations-snapshot-v1.schema.json` 约束的 aggregate-only 数据，Dashboard 只读本地
快照。目标 ModelPort 权限与拒绝矩阵定义在 `contracts/modelport-operations-v1.json`。

当前 ModelPort 尚未实现 `operations:aggregate:read` 专用 scope 时，Collector 使用管理员兼容
适配器，`currentAdapterIsLeastPrivilege=false` 会保持显式。只有 ModelPort 实现该 scope、拒绝用户/
密钥/原始请求接口，并在两个仓库通过兼容测试后，才能切换为真正最小权限；本仓库不得通过改名
或代理管理员凭据伪造完成状态。

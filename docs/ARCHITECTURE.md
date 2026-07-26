# 架构与边界

## 请求路径

```text
应用 / Agent
  └─ 可选 ModelPort（Anthropic Messages、认证、路由、账务、Tool 协议）
       └─ qwen-runtime:8080/v1
            └─ llama.cpp CUDA + 本地 GGUF

本机诊断 ──> 127.0.0.1:18080/v1（OpenAI-compatible）
```

ModelPort 不是首次部署前置条件。应用负责真实工具执行、审批、沙箱、幂等和业务判断；
本仓库不执行工具。

## 职责

| 层 | 负责 |
| --- | --- |
| 本仓库 | 模型选择、制品完整性、运行时、容量 Profile、验收、候选发布、聚合运维 |
| ModelPort | 认证、逻辑模型、Anthropic/OpenAI 边界、Token 准入、Tool Use 适配 |
| 应用 / Agent | 上下文管理、工具选择与执行、权限控制、最终结果验证 |

权威数据源：

- `catalog/models.json`：模型、制品、哈希与容量；
- `compose.yaml` + `profiles/`：运行配置；
- `contracts/local-qwen-provider-v1.json`：跨仓库协议；
- `deployments/*/manifest.json`：已验证部署身份。

## 当前运行设计

| Profile | Slot | 每 Slot 上下文 | 用途 |
| --- | ---: | ---: | --- |
| `latency` | 1 | 128K | 默认，单任务和长上下文 |
| `throughput` | 2 | 64K | 显式切换，双任务吞吐 |

默认开启思考，建议渲染后输入不超过约 92K，最多输出 32,768 tokens。16GB 单卡不能
同时驻留生产和完整候选，因此发布采用停产后的串行候选，始终自动恢复生产。

## 设计约束

- 仅自动支持 Linux/WSL x86_64 NVIDIA 单 GPU。
- 服务只绑定 loopback；本项目不提供公网、多租户或高可用。
- llama.cpp 镜像、模型和 GGUF 均固定不可变身份。
- 推荐、下载、上下文准入和验收默认 fail-closed。
- `validated` 档位不等于新主机已验证；本机凭证必须仍然匹配且有效。
- 运行时使用 OpenAI-compatible；Anthropic Messages 由可选 ModelPort 提供。
- 生产保留 Q8_0 KV、单 Slot、文本模型和有界缓存；其他组合必须候选验收。
- ModelPort 契约变更必须协调两个仓库，不能在本仓库复制其实现。

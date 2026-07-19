# 架构与容量设计

## 目标

在当前工作站上提供一个可重复部署、可回滚、仅本机可访问的
Qwen3.5-9B Q5_K_M 服务，并通过 ModelPort 暴露 Anthropic 兼容接口。

本阶段包含文本推理、128K 上下文、默认启用的 Reasoning、OpenAI 兼容 API、
ModelPort 路由和监控。视觉投影器会下载并校验，但基线服务不加载；
MTP 和公网服务不启用；双 Slot 仅作为显式 `throughput` profile，不是生产默认。

## 请求路径

```text
Claude Code / Anthropic SDK / 本地应用
                 |
                 | Anthropic Messages API
                 v
      ModelPort 127.0.0.1:38082
                 |
                 | OpenAI Chat Completions
                 | Docker network: modelport_default
                 v
       qwen-runtime:8080 (OpenAI-compatible)
                 |
                 v
       Qwen3.5-9B-Q5_K_M.gguf
```

llama.cpp 同时发布独立宿主机端口 `127.0.0.1:18080`，只用于本机诊断和验收。正常业务流量
应通过 ModelPort，以保留认证、路由、配额、审计和协议转换能力。

只读模型运行台位于 `127.0.0.1:33004`，从 llama.cpp、ModelPort、Docker、
`nvidia-smi` 和聚合快照读取运行信号；它不在业务请求链路内，异常或重启不会影响
推理服务。页面通过同源 WebSocket 接收 2 秒瞬时采样、5 秒窗口聚合和 30 秒趋势
同步，连接断开后自动重订阅。ModelPort 原管理界面继续使用 `127.0.0.1:33002`。

## 容量预算

Q5_K_M 文件约 6.13GiB。Qwen3.5-9B 的 32 层中只有 8 层使用完整注意力，
单个 128K Slot 的 KV Cache 约为：

```text
F16  = 4.00 GiB
Q8_0 = 2.12 GiB
Q4_0 = 1.12 GiB
```

当前采用 Q8_0，在质量、显存和长上下文之间留出平衡。加上权重、CUDA
上下文、计算图和批处理缓冲，优化验收期间整卡峰值约为
12,399MiB，剩余 3,597MiB。这已包含 WSL/Windows 图形环境的显存占用，
但仍应避免同时运行其他重度 GPU 工作负载。

## 上下文预算

`131072` 是渲染后输入、Reasoning 和最终输出的总和。应用层建议：

| 场景 | 最大输入 | 最大输出 |
| --- | ---: | ---: |
| ModelPort / Agent 默认思考 | 约 92,000 | 32,768 |
| 复杂推理，增加安全余量 | 约 81,920 | 32,768 |
| 非思考容量验收 | 约 118,000 | 8,192 |

服务禁用 context shift。超限请求应明确失败，而不是静默丢弃早期上下文。

## Reasoning 策略

服务通过 `--reasoning on` 默认开启思考，并配置预算耗尽提示，满足 ModelPort、
Claude Code 和 Agent 的日常思考需求。此前 118K 输入同时只留 8K 输出时，模型
可能把预算全部用于思考；因此不关闭能力，而是将生产输入收敛至约 92K，并把
默认生成上限扩大到 32K。128K 仍作为总容量和非思考长文检索能力保留。

直连 llama.cpp 的低延迟任务可显式关闭：

```json
{"chat_template_kwargs":{"enable_thinking":false}}
```

`scripts/reasoning-smoke.sh` 保证默认配置仍会返回独立 `reasoning_content` 和最终
正文。ModelPort 将 Anthropic `thinking` 映射到 llama.cpp 的请求级开关与预算，
并提供 `qwen3.5-fast`、`qwen3.5-code`、`qwen3.5-deep` 三个逻辑预算档位。

## 性能策略

- `batch-size=2048`、`ubatch-size=1024`：96K 预填充比 `ubatch=512` 快约 5.2%，
  只增加约 190MiB 显存；`batch=4096` 没有进一步收益。
- `cache-ram=8192`：容纳更多仓库和会话的精确前缀；92K 前缀重复请求实测从
  41.83 秒降到 7.37 秒。
- `spec-type=ngram-mod`：多样化输出平均影响约 +0.7%，重复结构输出最高从约
  88--90 tok/s 提升到 126.59 tok/s，适合代码和 Agent 循环。
- ModelPort 逻辑别名注入任务采样：`qwen3.5-code` 使用精确编码参数，fast/deep
  使用通用思考参数，客户端显式值优先。
- 双 Slot `throughput` profile 将两路聚合吞吐从 82.60 提升到 138.71 tok/s；
  默认单 Slot `latency` profile 保留完整 128K 和更高单请求速度。

## 安全边界

- Qwen、ModelPort、两个管理界面的宿主机端口都只绑定 `127.0.0.1`。
- llama.cpp 容器只读运行、删除 Linux capabilities，并启用
  `no-new-privileges`。
- 模型以只读卷挂载；CUDA 缓存单独写入 `cache/`。
- llama.cpp 不启用内置文件或 Shell 工具。
- 模型就绪后使用离线模式，不在推理时访问 Hugging Face。
- ModelPort 保持现有 API Token 和管理认证。

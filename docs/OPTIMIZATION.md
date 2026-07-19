# 推理优化方案与实测

## 最终生效配置

当前目标不是让每个请求都占满 128K，而是在 16GB 显存上保留 128K 能力，同时
让默认思考、代码生成和 Agent 循环更稳定：

| 项目 | 生效值 | 目的 |
| --- | --- | --- |
| 总上下文 / Slot | 131,072 / 1 | 单用户长上下文优先 |
| 生产思考输入预算 | 约 92K rendered tokens | 为思考和正文留出空间 |
| 最大生成量 | 32,768 | 避免长思考挤掉最终正文 |
| KV Cache | K/V `q8_0` | 兼顾质量与显存 |
| Batch / uBatch | 2048 / 1024 | 提高长提示预填充吞吐 |
| Prompt cache | 8GiB RAM | 容纳更多稳定系统提示和会话前缀 |
| Speculative | `ngram-mod` | 加速重复代码和 Agent 结构 |
| Thinking | 默认开启 | 满足 ModelPort 日常使用 |

## A/B 结果

### 长提示预填充

使用约 96K 输入、关闭思考并精确召回同一验收码：

| Batch / uBatch | 端到端 | 服务端预填充 | 显存占用 / 空闲 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 2048 / 512 | 34.78 s | 2,798.72 tok/s | 12,209 / 3,787 MiB | 基线 |
| 2048 / 1024 | 33.05 s | 约 2,945 tok/s | 12,399 / 3,597 MiB | 采用，约快 5.2% |
| 4096 / 1024 | 33.17 s | 2,943.11 tok/s | 12,380 / 3,616 MiB | 无收益，不采用 |

### 解码与 n-gram 推测

512-token、关闭思考的多主题测试中，无推测平均约 87.98 tok/s，`ngram-mod`
平均约 88.59 tok/s，差异约 +0.7%。重复相同生成结构时，基线稳定在约
88--90 tok/s；`ngram-mod` 随缓存学习最高达到 126.59 tok/s。

因此保留 `ngram-mod`：普通多样化请求基本不受损，代码模板、工具循环和重复
Agent 轨迹可能明显获益。它不是独立小模型 speculative decoding，不额外占用一份
draft 模型显存。

### 默认思考与提示缓存

最终配置经 ModelPort Anthropic Messages 验收：

| 场景 | 输入 | 输出 | 端到端 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 92K 冷缓存、默认思考 | 92,063 | 554 | 39.26 s | 精确召回 |
| 相同 92K 前缀热缓存 | 92,063 | 552 | 7.37 s | 精确召回 |
| 118K 容量、关闭思考 | 118,062 | 20 | 42.61 s | 精确召回 |

热请求延迟下降约 81%。为了提高命中率，应用应保持 system prompt、工具说明和
仓库规则的顺序与内容稳定，将当前问题、时间戳和动态检索结果放在尾部。

## 采样配置

服务全局采用 Qwen 官方通用思考建议：`temperature=1.0`、`top_p=0.95`、
`top_k=20`、`min_p=0`、`presence_penalty=1.5`、`repeat_penalty=1.0`。
精确编码、数学或指令遵循任务可按请求覆盖为 `temperature=0.6`、
`top_p=0.95`、`top_k=20`、`presence_penalty=0`；确定性验收仍使用温度 0。

Qwen3.5 不使用 `/think`、`/nothink` 文本开关；直连 llama.cpp 时通过
`chat_template_kwargs.enable_thinking` 控制。ModelPort 会把 Anthropic `thinking`
映射为该开关和 `thinking_budget_tokens`。

## 未采用或暂缓

- `batch=4096`：未提高 96K 预填充吞吐。
- 双 Slot：会把 128K 上下文和 KV 显存拆分，违背当前单用户长上下文目标。
- Q4 KV：K 或 V 任一改为 q4 后，92K 预填充都从约 3,100 tok/s 退化到约
  200 tok/s 量级，已明确淘汰。
- MTP：同量化 MTP 制品已完成 CUDA A/B。`n=2` 短解码提高约 6.4%，但 92K
  预填充和解码分别回退约 13.6% 和 8.1%，显存升至 13,085MiB，因此不替换生产制品。
- 视觉 mmproj：会占用额外权重和临时显存，继续作为独立 profile 的后续工作。

## 本轮已实现与后续路线

以下记录本轮已实施的改动、被实测淘汰的候选，以及仍可继续推进的项目。

### P0 已实现：精确 Token 预算接口

llama.cpp 当前原生提供 Anthropic `POST /v1/messages/count_tokens`。ModelPort 已将
它沉淀为默认关闭、按 Provider 显式开启的通用 capability，并对 `local_qwen`
启用。请求继续使用 Anthropic 的 system、messages、tools 与 Tool Use 校验，逻辑
别名会重写为真实上游模型；返回值只接受上游的整数 `input_tokens`。

这解决了中文、Tool Schema 和聊天模板无法由“字符数除以 4”可靠估算的问题。
计数请求不做跨 Provider fallback，也不进入推理用量账本。应用应先拼好完整请求再
计数，硬限制使用 `input_tokens + max_tokens <= 131072`；默认思考生产目标仍保持
约 92K 输入，而不是把精确计数误解为可以占满全部上下文。

### P0 已实现：请求级思考预算和逻辑模型档位

当前 llama.cpp 支持请求字段 `thinking_budget_tokens`。本机实测传入 32 和 128
时，`reasoning_content` 分别约为 31 和 127 tokens，说明预算能够生效；但 32
过小会导致正文质量下降并耗尽 512 总输出，不能作为生产默认值。

ModelPort 已增加配置驱动的通用 OpenAI-compatible reasoning 映射，adapter 中不
硬编码 Qwen 名称：

| 逻辑档位 | 初始思考预算 | 初始总输出 | 用途 |
| --- | ---: | ---: | --- |
| `qwen3.5-fast` | 512 | 4,096 | 分类、简单工具选择、短问答 |
| `qwen3.5-code` | 4,096 | 8,192--16,384 | 日常代码与 Agent |
| `qwen3.5-deep` | 16,384 | 32,768 | 复杂调试、架构和长链推理 |

Anthropic 请求中的 `thinking.budget_tokens` 由 provider capability 映射到
llama.cpp 的 `thinking_budget_tokens`。未显式提供时使用逻辑模型档位默认值；
远程 provider 继续使用各自原生字段。预算值必须通过真实代码、数学、Tool Use
和长上下文任务校准，不能仅凭 token 越多越好。

服务已改用 `--reasoning on`，并以 `--reasoning-budget-message` 在预算耗尽时引导
模型收束为最终答案。实测 128-token 强制预算不再把 `</think>` 泄漏到正文；
`thinking.type="disabled"` 可按请求关闭。逻辑别名默认预算为 fast 512、code
4,096、deep 16,384，显式客户端预算优先。

### P0 已实现：扩大主机提示缓存并保持稳定前缀

服务的 2GiB prompt cache 曾出现逐出 100--210MiB cache entry 的日志；WSL 当前
有充足内存，因此已扩大到：

```text
--cache-ram 8192
```

`n_cache_reuse=256` 已用 113,409-token、前部约 8% 发生变化的请求验证。当前
Qwen3.5 混合上下文后端明确记录 `cache reuse is not supported` 并忽略参数，因此
生产不启用。8GiB 精确前缀缓存继续生效；应用侧应保持 system prompt、工具 schema、
仓库规则顺序稳定，把时间戳、检索结果和当前问题放在消息尾部。

### P1 已验证、未采用：Qwen3.5-9B MTP GGUF

上游包含 MTP heads 的 Q5_K_M GGUF 已作为独立制品下载并校验，llama.cpp
使用 `draft-mtp` 完成了 `n=6` 与 `n=2` A/B：

```text
--spec-type draft-mtp
--spec-draft-n-max 2 / 6
```

| 配置 | 短 decode | 92K prefill | 92K decode | 显存 | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| 普通 GGUF + ngram | 87.74 tok/s | 3,102.36 tok/s | 64.26 tok/s | 11,946MiB | 生产基线 |
| MTP `n=6` | 54.61 tok/s | — | — | — | 接受率 26.4%，淘汰 |
| MTP `n=2` | 93.34 tok/s | 2,679.87 tok/s | 59.06 tok/s | 13,085MiB | 长上下文回退，淘汰 |

MTP 文件保留，待 llama.cpp CUDA/MTP 内核升级后用同一脚本复测，不作为当前默认。

### P1 已验证、未采用：长上下文 KV 量化矩阵

92K 时 decode 从短上下文约 88--90 tok/s 降至约 63.5 tok/s，长 KV 读取已经成为
明显成本。测试以下矩阵，目标是提升长上下文 decode，而不只是节省显存：

| K cache | V cache | 目的 |
| --- | --- | --- |
| q8_0 | q8_0 | 当前质量基线 |
| q8_0 | q4_0 | 先压缩 V，保守候选 |
| q4_0 | q8_0 | 压缩 K 的边界验证 |

`q8_0/q8_0` 的 92K 召回以 3,134.17 tok/s 预填充通过。`q8_0/q4_0` 与
`q4_0/q8_0` 都在早期预填充阶段降至约 200 tok/s，远未达到性能门槛，测试提前
终止。当前后端的 q4 KV 路径不适合此混合架构，继续保留 Q8_0。

### P1 已实现：按任务注入采样参数

当前全局参数适合通用思考，Qwen 官方对精确编码建议
`temperature=0.6, presence_penalty=0`。ModelPort 现已按请求中的逻辑别名同时
注入思考预算与采样参数：

| 档位 | temperature / top_p | top_k / min_p | presence / repeat | 思考预算 |
| --- | --- | --- | --- | ---: |
| `qwen3.5-fast` | 1.0 / 0.95 | 20 / 0.0 | 1.5 / 1.0 | 512 |
| `qwen3.5-code` | 0.6 / 0.95 | 20 / 0.0 | 0.0 / 1.0 | 4,096 |
| `qwen3.5-deep` | 1.0 / 0.95 | 20 / 0.0 | 1.5 / 1.0 | 16,384 |

这些别名共享同一个 llama.cpp 进程和权重，不增加显存。实现是 provider 配置驱动
的通用 capability，没有硬编码模型名；客户端显式传入的采样值优先，未列入 profile
的模型保持不变。三个档位的非流式真实上游请求与 `qwen3.5-code` Tool Use 均通过。

### P1 已实现：Tool Use 完整参数提交与响应侧 fail-closed

本机原始 SSE 已确认函数参数按标准 delta 拆分，并以 `finish_reason=tool_calls`
结束。为避免客户端在完整 JSON 和工具名尚未验证前执行调用，`local_qwen` 改为
`streaming_arguments="best_effort"` 与 `response_validation="strict"`：参数在
ModelPort 内聚合为完整对象后提交，未声明/缺失工具名、非法或非对象 JSON、重复
调用 ID、超出单调用约束及 finish reason 不一致都会被拒绝。入口同时校验
`input_schema` 必填、`tool_result` 必须紧邻且先于文本，并把 Anthropic
`is_error=true` 显式标记进 OpenAI tool-role 内容。该实现位于通用协议 adapter，
Dashboard 也可按 Provider 配置，不绑定 Qwen。

### P2 已实现：并发吞吐 profile

现已提供 `latency` 和 `throughput` 两个显式可切换 profile。相同两路并发、每路
生成 512 tokens 的实测结果：

| Profile | Slot | 每 Slot 上下文 | 双请求墙钟 | 聚合吞吐 | 单请求 decode |
| --- | ---: | ---: | ---: | ---: | ---: |
| `latency` | 1 | 128K | 12.397 s | 82.60 tok/s | 86--88 tok/s，串行 |
| `throughput` | 2 | 64K | 7.382 s | 138.71 tok/s | 约 70.99 tok/s |

双 Slot 聚合吞吐提升约 67.9%，显存约 12,177MiB，但单请求慢约 18%，且上下文减半。
因此生产默认已恢复 `latency`；只有两个 Agent 同时工作且输入不超过各自预算时才
切换 `throughput`。

```bash
scripts/runtime.sh profile throughput
scripts/runtime.sh profile latency
```

### P2：原生 Blackwell 构建和空闲首请求

固定 CUDA 镜像已经复用 CUDA graphs，原生 `sm_120` 构建的增益可能很小。只有在
前述项目完成后，才用完全相同的模型和请求比较通用镜像与原生构建。另有一次空闲
数分钟后的首请求从约 89 tok/s 短暂降到约 23 tok/s；可单独测试 Windows NVIDIA
“最高性能优先”或低频 keep-warm，请同时评估功耗，不把它默认启用。

### P2 已实现：发布门禁、KV 快照与最小权限

固定质量集现通过 ModelPort 真实入口覆盖文本、代码、JSON 和 Tool Use，发布要求全部
Case 重复三次；本轮为 30/30。16GB 单卡使用 `18081` 串行候选，脚本通过 trap 在
成功、失败和中断后清理候选并恢复原生产状态，避免把无法同时驻留的两份 128K 实例
伪装成蓝绿部署。

llama.cpp 的显式 Slot Save/Restore 已接入本地敏感缓存目录。合成前缀实测保存 140
tokens、写入 55,132,240 bytes，恢复读取量一致；它适合后续固定 Agent 前缀冷启动
A/B，不自动用于真实会话。为使缓存目录不放宽为全局可写，Runtime 同时降权到
UID/GID `1000:1000`，继续保持只读根文件系统、drop ALL 和 no-new-privileges。

候选/恢复演练后立即运行相同 512-token 解码两次，冷请求为 81.36 tok/s，热请求为
120.18 tok/s，进一步确认 `ngram-mod` 的收益主要出现在重复结构和热路径；日常容量
规划仍使用较保守的多样请求基线，不用 120 tok/s 作为所有请求承诺。

## 后续 A/B 观测指标

每个候选至少记录：冷/热响应开始时间、真实流式 TTFT、prompt tok/s、短/92K decode tok/s、draft
acceptance、cache reused tokens、cache eviction、reasoning tokens、峰值显存、主机
RAM、正文完成率、Tool 协议通过率、闭环任务成功率和 118K 召回。性能通过但质量
验收失败的参数不进入生产配置。

## 2026-07-19 增强审查

当前生产参数继续作为稳定基线：Qwen 官方采样已经按 fast/code/deep 档位落地，
128K 为 Thinking 保留足够上下文，q4 KV 与 MTP 也已经被本机 A/B 否决。进一步收益
主要来自完整 Tool Schema 校验、闭环工作流观测、验证器反馈、上下文工作集控制和
真实 TTFT，而不是继续堆叠 llama.cpp 开关。

下一阶段先实施 Tool Use Reliability v2，再实施验证器驱动的
`fast -> code -> deep` 自适应升级；Q6_K 权重、MTP 复测、原生 `sm_120` 与引擎升级
保持候选实验。完整优先级与门槛见
[`ENHANCEMENT_ROADMAP.md`](ENHANCEMENT_ROADMAP.md)。

## 复验命令

```bash
cd /home/tiammomo/projects/infra/local-inference-stack

scripts/smoke-test.sh
scripts/reasoning-smoke.sh
scripts/modelport-smoke.sh
scripts/modelport-reasoning-smoke.sh
scripts/modelport-context-acceptance.sh
DECODE_CONTEXT_TOKENS=92000 python3 scripts/decode-benchmark.py

cd /home/tiammomo/projects/dev/ModelPort
scripts/provider-matrix.sh --model qwen3.5-code
scripts/tool-use-acceptance.sh --upstream --max-tokens 2048
```

Tool Use 流式参数允许拆分为多个 `input_json_delta.partial_json`；测试必须拼接后
再解析，不能要求单一事件包含完整 JSON。

## 参考

- [Qwen3.5-9B 官方 README](https://huggingface.co/Qwen/Qwen3.5-9B/blob/main/README.md)
- [llama.cpp speculative decoding](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
- [llama.cpp server 参数](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

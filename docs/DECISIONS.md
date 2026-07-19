# 技术决策

## D1：使用 llama.cpp，而不是 vLLM/SGLang

状态：已采用。

目标制品是 GGUF Q5_K_M。llama.cpp 对 GGUF、量化 KV、消费级单 GPU 和
CPU/GPU 混合卸载支持更直接。若未来改为 BF16/AWQ、4 个以上并发或连续批处理
吞吐优先，再重新评估 vLLM/SGLang。

## D2：使用固定 CUDA 容器，而不是本机编译

状态：已采用。

当前 WSL 有 Docker、NVIDIA runtime 和可用 GPU，但没有 CMake/NVCC。固定 OCI
digest 能减少宿主依赖并提供明确回滚点。需要追踪 llama.cpp 主线修复时，可在
独立镜像中构建，不覆盖已验收镜像。

## D3：128K 单 Slot、Q8_0 KV

状态：已采用。

单用户/Agent 的完整 128K 比未经验证的并发更重要。Q8_0 将单 Slot KV 从约
4GiB 降至约 2.12GiB，并比 Q4_0 保留更多质量余量。118K 实测后仍保留
约 3.6GiB 整卡显存余量。本基线不启用双并发；并发需求应优先用请求队列，
或降低每 Slot 上下文后重新验收。

## D4：基线不加载视觉投影器

状态：已采用。

视觉投影器已下载并校验，以便后续启用。文本 Agent 是当前接入 ModelPort 的
主场景；先将约 0.86GiB 权重和视觉临时缓冲留给长上下文。启用视觉时需要新的
Compose profile 和独立验收记录。

## D5：业务 Provider 与推理引擎身份分离

状态：已采用。

ModelPort 使用部署级 `local_qwen` provider 和具体展示名，`infra/local-inference-stack` 对外只
承诺稳定的 OpenAI-compatible 端点及 model ID。llama.cpp 是可替换实现，不进入
路由身份；协议 adapter 的修改仍必须服务于所有本地 OpenAI-compatible runtime，
并附带测试和文档。

## D6：默认开启思考，以预算而非关闭能力控制风险

状态：已采用。

118K 输入只留 8,192 输出时，Qwen 可能耗尽预算而没有最终正文；这说明输入和
输出分配不合理，不代表应关闭思考。服务通过 `--reasoning on` 默认开启思考，
ModelPort 提供请求级预算，生产输入
建议约 92K，服务端最多允许 32,768 输出。直连低延迟请求仍可显式关闭思考。

## D7：采用 2K/1K 批处理、提示缓存和轻量推测解码

状态：已采用。

96K A/B 中，`batch=2048, ubatch=1024` 比 `ubatch=512` 的预填充吞吐高约 5.2%，
只多用约 190MiB；`batch=4096` 没有可测收益。提示缓存现扩为 8GiB RAM，重复 92K
前缀的延迟从 41.83 秒降到 7.37 秒。`ngram-mod` 在多样化输出中与基线基本持平，
在重复结构生成中有明显收益，因此适合本机代码和 Agent 工作流。若未来启用多
Slot 或 GPU 同时承担其他任务，需重新评估这些参数。

## D8：运行时用 OpenAI-compatible，应用侧保留 Anthropic Messages

状态：已采用。

llama.cpp 原生暴露 OpenAI Chat Completions，并用扩展字段表达思考开关、预算和
`reasoning_content`；ModelPort 继续向 Claude Code 与 Anthropic SDK 暴露 Messages
API。ModelPort 的通用 reasoning capability 将 Anthropic `thinking` 映射到
llama.cpp 扩展。协议边界因此与各层原生能力一致，应用不直接依赖 llama.cpp。

## D9：生产保留 Q8 KV 与 ngram，不启用 q4 KV、cache reuse 或 MTP

状态：已采用。

选择依据是本机 92K A/B，不是特性存在与否。q4 K/V 路径将预填充吞吐从约
3,100 tok/s 降到约 200 tok/s；当前混合后端明确不支持非精确 cache reuse；MTP
`n=2` 虽使短解码提升约 6.4%，但 92K 预填充/解码分别回退约 13.6%/8.1%，并多用
约 1.1GiB 显存。生产因此继续使用 q8/q8 + ngram，MTP 制品仅留作引擎升级复测。

## D10：逻辑模型携带采样档位，并将双 Slot 作为显式切换项

状态：已采用。

ModelPort 的 sampling capability 按原始请求别名注入配置，显式客户端参数优先，
远程 provider 和未匹配模型不受影响。`qwen3.5-code` 使用 Qwen 官方精确编码采样，
fast/deep 使用通用思考采样。双 Slot 在两路 512-token A/B 中把聚合吞吐从 82.60
提高到 138.71 tok/s，但牺牲单请求速度和一半上下文，因此只作为 `throughput`
profile，生产默认保持 `latency` 单 Slot 128K。

## D11：上下文预算使用上游精确计数，不用字符近似做硬拦截

状态：已采用。

ModelPort 原有“序列化字符数除以 4”只适合作为缺少上游 usage 时的计费估算，对
中文、Tool Schema 和聊天模板都不够准确，不能据此拒绝请求。当前将 Anthropic
Count Tokens 沉淀为显式 Provider capability；`local_qwen` 通过 llama.cpp 的 Qwen
tokenizer 返回精确 `input_tokens`。逻辑别名和思考开关会按实际生成路径映射，计数
不跨 Provider fallback，也不产生推理账单。容量硬条件使用精确计数，思考模式仍
保留约 92K 的生产输入目标和 32K 最大输出余量。

## D12：本地 Tool Use 使用聚合恢复加严格响应校验

状态：已采用。

llama.cpp 实测输出标准 OpenAI 增量 `tool_calls`，但 Agent 最终执行工具前仍需要
网关侧安全边界。`local_qwen` 因此使用
`streaming_arguments="best_effort"` 聚合/恢复完整 JSON，并使用
`response_validation="strict"` 校验工具名必须来自本次声明、参数必须是 JSON
对象、调用 ID 不重复、调用数量符合 `tool_choice`/并行约束且 finish reason 一致。
这两项是 ModelPort 的通用 Provider capability，不含 Qwen 专用字符串解析；远程
Provider 默认仍为 `best_effort`，需通过真实验收后再选择严格模式。

## D13：上下文准入 fail-closed，客户端取消不计入服务失败

状态：已采用。

ModelPort 在占用推理 Slot 和创建计费租约前调用上游精确 Count Tokens。总预算超过
131,072，或开启思考且输入超过 94,208 时返回带精确数值的 400；不做静默截断。
可观测口径把 `downstream_cancelled` 单独归类，服务可用率不把客户端主动取消算作
服务失败，同时保留原始调用成功率以观察完整生命周期。

## D14：16GB 单卡采用串行候选，不伪装蓝绿并行

状态：已采用。

两份 128K Runtime 无法同时稳定驻留，因此候选使用独立 Compose project、容器、
端口 `18081` 和 Cache，但与生产串行占用 GPU。发布脚本无论通过、失败或中断都会
清理候选，并仅在生产原本运行时恢复生产。质量门禁和长上下文回归是晋级条件，单纯
提高吞吐不构成发布理由。

## D15：下一阶段优先闭环可靠性，不继续无证据堆叠运行参数

状态：已采用为演进原则，功能待分批实现。

2026-07-19 审查确认当前 128K、Q8 KV、Q5_K_M、采样和单 Slot 基线稳定；主要缺口
已经从运行时参数转向 Tool 参数 Schema 校验、工作流终态、真实 TTFT、测试流量标签
和推理验证反馈。下一阶段先在 ModelPort 建立完整 JSON Schema 校验、一次有边界的
协议修复和闭环枚举状态，再由本项目扩展真实上游质量集与运行台。业务 Tool 的执行、
审批、沙箱和幂等继续属于应用层，ModelPort 不成为任意 Tool Executor。

128K 保持为 Thinking 容量上限，日常工作集通过压缩和检索控制；Q6_K、MTP、原生
CUDA 构建与引擎升级只走 `18081` 串行候选。详细顺序和门槛见
`ENHANCEMENT_ROADMAP.md`。

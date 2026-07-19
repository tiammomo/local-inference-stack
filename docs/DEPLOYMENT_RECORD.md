# 实际部署记录

更新时间：2026-07-19（Asia/Shanghai）

## 主机

| 项目 | 实际值 |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5070 Ti，16303 MiB |
| 驱动 | 595.97 |
| CUDA capability | 12.0 |
| CPU | AMD Ryzen 7 9800X3D，8C/16T |
| 物理内存 | 96GB |
| WSL 可见内存 | 约 70GiB，另有 32GiB swap |
| OS | Ubuntu 24.04.1 LTS / WSL2 |
| 磁盘 | 下载 MTP A/B 制品后工作区可用约 840GB |

## 制品

| 项目 | 实际值 |
| --- | --- |
| llama.cpp OCI digest | `sha256:0d6c600a69e8bdaafd7b91ed6db9160906ee8148ee12a609cf4d52b4e17aabe8` |
| llama.cpp build | `10015`，commit `12127defd` |
| ModelPort 源码 | 本地 commit `c115364`（本轮未推送 ModelPort 远端） |
| ModelPort 本地镜像 ID | `sha256:e122556bdb4e460db417ec187b252a210c226da700c8ba3568725028e9513d5b` |
| Dashboard 本地镜像 ID | `sha256:99cb2838e274b4042a3b7fbe4842ad0370b8a83ec5a767eb70df44c343f1b850` |
| Q5_K_M SHA256 | `dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c` |
| MTP Q5_K_M SHA256 | `1732d6616554b102be9bc41684cd094f471e1b3067f5e5a89eb5a86a5a4f2a6c`，仅保留用于 A/B |
| mmproj SHA256 | `853698ce7aa6c7ba732478bad280240969ddf7b0fcbf93900046f63903a83383` |

## 验收结果

| 测试 | 结果 | 备注 |
| --- | --- | --- |
| Docker GPU | 通过 | CUDA 13.0.1 基础镜像可见 RTX 5070 Ti |
| 模型校验 | 通过 | Q5_K_M 和 mmproj 的 SHA256 均匹配 |
| llama.cpp 健康 | 通过 | `/health` 200；1 Slot，`n_ctx=131072` |
| 独立端口 | 通过 | Qwen `127.0.0.1:18080`；旧宿主机端口 `8080` 已关闭；ModelPort `38082`；运行台 `33004` |
| GPU 卸载 | 通过 | CUDA 后端，全部模型层 offload，Flash Attention/Q8_0 KV |
| 直连生成 | 通过 | OpenAI Chat Completions 返回预期中文 |
| 显式 Reasoning | 通过 | 请求级开启后同时返回 `reasoning_content` 和正确正文 |
| ModelPort 思考映射 | 通过 | 128-token 预算返回 42/138 output tokens；关闭思考返回 42/3 output tokens，无标签泄漏 |
| ModelPort 采样档位 | 通过 | fast/code/deep 均可路由；code 使用精确编码参数；显式请求参数优先 |
| ModelPort 生成 | 通过 | Anthropic Messages 经 `local_qwen` 返回预期中文 |
| ModelPort 流式 | 通过 | provider matrix 的非流式与流式均 PASS |
| ModelPort Tool Use | 通过 | 严格模式下非流式、完整参数流式与 continuation 均 PASS；Mock 拒绝未声明工具、非对象参数及完整 JSON Schema 违规 |
| 闭环 Tool Use | 通过 | 5 Case standard 冒烟 5/5；当前多步 Harness 全量 40/40（`logs/quality/20260719T063452Z-tool-workflow-full.json`），覆盖选择、Schema、Mock 执行、`tool_result` 续轮和 auto 不调用 |
| Tool 参数受控修复 | 通过 | Rust 顺序 Mock 首次 strict Schema 失败、第二次恢复；`retryCount=1`，22/5 Token 合并，attempted/recovered 为真且不误记 fallback；真实 Qwen 合法调用无需触发修复 |
| Tool 韧性集 | 通过 | 4/4：两工具依赖链、`is_error=true` 换仓恢复、Tool Result 指令注入防护、约 32KB 大结果摘要；分别为 3/3/2/2 轮 |
| 合成流量隔离 | 通过 | 1 小时加载 339 条、按 `trafficClass=synthetic` 与旧 Mock 兼容规则排除 107 条；228 条业务窗口成功率 100%，可用 `--include-synthetic` 审计 |
| 长期运营报告 | 通过 | 业务、synthetic、diagnostic 使用有界枚举；默认业务 SLO 排除 synthetic，并显示 Tool 修复恢复/尝试数 |
| 每日报告 Timer | 通过 | user systemd 已启用；迁移后执行 `status=0/SUCCESS`；每日 02:15 后随机 0--10 分钟，当前 `active (waiting)` |
| 本地模型运行台 | 通过 | `127.0.0.1:33004`；systemd active/enabled；2048px 满宽和 390px 移动端无溢出、无浏览器错误 |
| WebSocket 实时链路 | 通过 | 前端无 HTTP API 轮询；实测 2.0--2.2 秒 live、5 秒 status；6H 订阅、强制刷新和服务重启自动重连通过 |
| 运行台可读性与数据范围 | 通过 | 关键字号分层放大；Qwen 路由仅显示当前部署和 `local_qwen`；成功率/P95 双轨趋势在 2048px 与 390px 验收通过 |
| 项目身份与路径迁移 | 通过 | 项目改为 Local Inference Stack；代码、systemd、Docker bind mount 与 Compose 元数据均已迁至 `infra/local-inference-stack`；旧 `infra/models` 入口已移除 |
| 统一 Quick 验收 | 通过 | 新路径下运行时、直连生成、Reasoning、ModelPort Messages、精确 Token 和运行台全部通过 |
| Qwen 统计口径 | 通过 | 运行台与每日报告按 `provider=local_qwen` 过滤；当前 24H 只保留 `qwen3.5-9b-q5km`，不混入其他 ModelPort 模型 |
| 运行台只读边界 | 通过 | 仅回环监听；POST 405；非法窗口 400；越界/缺失资源 404；WebSocket 非同源 Origin 403；安全响应头生效 |
| ModelPort 精确 Token 计数 | 通过 | 中文 system、混合消息和 Tool Schema：直连与逻辑别名均为 282；关闭思考模板均为 15 |
| ModelPort 上下文准入 | 通过 | `15 + 131072 > 131072` 在占用 Slot 前返回 400；错误含精确数值和“不静默截断”保证；思考输入建议上限 94,208 |
| 部署漂移检查 | 通过 | 39/39；模型、镜像、构建、上下文、端口、挂载、基础/闭环/韧性质量 SHA256、最小权限和跨项目契约一致 |
| 合成质量门禁 | 通过 | 10 个 Case × 3 次，共 30/30；覆盖推理、指令、抽取、JSON、代码、多语言和 Tool Use |
| Tool Use 结果观测 | 通过 | 请求级区分 `tool_called/final_answer/answered_without_tool/completed_unobserved` 与错误终态；不保留工具内容 |
| 聚合时序历史 | 通过 | SQLite `0600`；24h 原始、30d 分钟和 365d 小时保留；WebSocket 查询按窗口自动选分辨率 |
| 启动自愈 | 通过 | `qwen-model-runtime.service` 已 enabled/active；健康时幂等退出，缺失时等待 ModelPort network 后恢复 |
| Runtime 最小权限 | 通过 | UID/GID `1000:1000`、只读根文件系统、drop ALL、no-new-privileges、仅回环端口 |
| KV 快照实验 | 通过 | Slot 0 合成前缀保存/恢复成功；140 tokens、55,132,240 bytes、文件权限 `0600` |
| 串行候选/恢复演练 | 通过 | `18081` 候选经直连、ModelPort、Reasoning、Token、准入、Tool Use 和质量 4/4；候选清理后生产自动恢复 |
| ModelPort 基线仓库检查 | 通过 | 307 个 Rust lib 测试与 6 个配置测试通过；Clippy `-D warnings`、Mock 修复、Schema 拒绝和真实上游验收通过 |
| 118K 上下文 | 通过 | 冷缓存 118,062 prompt tokens，准确召回中部验收码 |
| 长上下文延迟 | 42.61 s | 最终配置的请求端到端；服务端约 2,807 tok/s |
| ModelPort 92K 思考冷链路 | 通过 | 92,063 input / 554 output tokens，39.26 s，正文精确匹配 |
| ModelPort 92K 思考热链路 | 通过 | 相同前缀，552 output tokens，7.37 s，正文精确匹配 |
| 2026-07-19 全量复验 | 通过 | 118,062 prompt / 20 output 为 45.82s；ModelPort 92,063 input / 693 output 为 47.14s；两者均精确召回且无截断/OOM |
| 提示缓存 | 通过 | RAM 容量扩为 8GiB；相同 92K 前缀历史实测从 41.83 s 降到 7.37 s |
| 非精确 cache reuse | 未采用 | 113,409-token A/B 中后端明确报告不支持并忽略 `n_cache_reuse=256` |
| Q4 KV A/B | 未采用 | K 或 V 使用 q4 时，92K 预填充从约 3,134 tok/s 降到约 200 tok/s |
| MTP A/B | 未采用 | `n=2` 短解码 93.34 tok/s，但 92K prefill/decode 回退至 2,679.87/59.06 tok/s |
| 双 Slot A/B | 可选 profile | 聚合吞吐 82.60 → 138.71 tok/s（+67.9%）；每 Slot 64K，单请求约 70.99 tok/s |
| 最大实测槽位 | 118,097 tokens | 服务端 `n_tokens_max`，`truncated=0`，未 OOM |
| 显存峰值 | 12,399 MiB | 优化验收期间整卡观测峰值，最低剩余 3,597 MiB |
| 92K 冷预填充 | 约 3,102 tok/s | 标准制品长 decode 基准；ModelPort 输入计数 92,063 |
| 短请求生成速度 | 约 88--90 tok/s | 多样主题 512-token 解码；重复结构最高 126.59 tok/s |
| 重启后冷/热短解码 | 81.36 / 120.18 tok/s | 候选恢复生产后的连续两次 512-token 实测，验证 n-gram 热身收益 |
| Provider 身份与计费复验 | 通过 | Quant Key 调用 `local_qwen`；17 input / 4 output tokens；`upstream-returned`；内部费用 `$0.00000685` |
| 2026-07-19 Tool Reliability standard | 通过 | `logs/acceptance/20260719T063149Z-standard.json`；基础闭环 5/5、韧性 4/4、质量 4/4，最终 ModelPort 镜像健康 |

## 服务现状

- 项目唯一根目录：`/home/tiammomo/projects/infra/local-inference-stack`；旧路径
  `/home/tiammomo/projects/infra/models` 已在容器重建和标准验收通过后移除。
- llama.cpp：`http://127.0.0.1:18080`，容器 `qwen35-9b-q5km`；原宿主机端口 `8080` 已释放。
- ModelPort：`http://127.0.0.1:38082`，默认 provider 为 `local_qwen`。
- Dashboard：`http://127.0.0.1:33002`。
- 本地模型运行台：`http://127.0.0.1:33004`，user systemd 服务
  `qwen-model-operations-dashboard.service` 已启用并运行；同端口 `/ws` 提供
  2 秒瞬时数据和 5 秒调用聚合推送。
- 视觉 mmproj 已下载校验，但文本基线服务未加载。
- 运行台时序库：`logs/operations/history.sqlite3`，仅聚合值、有界保留、权限 `0600`。
- 发布候选：串行独立端口 `127.0.0.1:18081`，候选失败或中断后自动恢复原生产状态。

ModelPort 已按 `qwen-runtime` / `qwen3.5-9b-q5km` 契约重新构建。切换后的
reasoning/sampling/token-counting/Tool Use adapter 改动已通过 Docker release 编译、
304 个 Rust lib 测试、Mock 严格 Schema 拒绝和真实 Qwen standard 验收。Dashboard
typecheck、lint、88 个测试和生产构建也已通过。真实 Anthropic
Messages 冒烟确认 Token 来自上游 usage；ModelPort 按 provider 级内部费率持久化
请求时的价格快照，后续调价不会改写历史费用。当前快照为
`local-qwen-2026q3-v1`：输入 `$0.05/M`、输出 `$1.50/M`、Cache Write
`$0.05/M`、Cache Read `$0.01/M`。

ModelPort 请求日志现记录不含参数的 `toolUseRequested` 工作流标志，Dashboard 可
筛选并汇总 Tool Use，`scripts/operations-report.sh` 生成只含聚合指标的 0600 权限
快照。企业账本中有 1 条本次容器替换产生的历史
`lease_expired_unreconciled`，已确认 `chargeable=false` 并作为本机已知基线保留；
后续只有总数增加才触发告警，不删除或改写历史证据。

2026-07-19 Tool Use Reliability v2 第一批增加了完整 Tool JSON Schema 响应校验、
脱敏错误路径、模型 Tool 决策终态和流式首语义 TTFT。本项目增加 40 Case 闭环套件
（standard 运行 5 Case 冒烟）以及对应聚合报表/运行台字段。本机已完成 standard
验收、5/5 冒烟和 40/40 全量闭环，这组改动现作为生产基线运行。

## 验收说明

Qwen3.5 在 118K 输入、只留 8,192 输出时曾耗尽 Reasoning tokens 而没有最终
正文。当前服务通过 `--reasoning on` 默认开启思考，ModelPort 生产输入预算调整为
约 92K、最大输出调整为 32,768。118K 仅作为显式关闭思考的容量验收。长上下文
脚本要求正文必须精确等于验收码，避免将“Reasoning 中偶然提到验收码”误判为
通过；填充文本也明确要求收到问题后检索，避免诱导模型提前持续思考。

ModelPort 的 `POST /v1/messages/count_tokens` 现在使用上游 Qwen tokenizer 和当前
聊天模板精确计数。它不使用本地计费估算、不做跨 Provider fallback，也不计为一次
推理用量；默认思考场景仍以约 92K 输入为生产目标。

## 升级历史

2026-07-18 初始 64K/Q8_0 基线曾以 58,041 prompt tokens 在 17.91 秒内
通过召回，峰值前整卡约占用 10.9GiB。同日按实测结果升级到
128K/Q8_0，当前生效基线以上表为准。

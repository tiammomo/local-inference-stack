# Local Inference Stack 文档

本目录是本地模型推理与运营栈的唯一规划与运维文档入口。当前生效部署档案是
Qwen3.5-9B Q5_K_M / RTX 5070 Ti / 128K。

## 文档导航

- [PROJECT.md](PROJECT.md)：项目定位、分层边界、命名与演进路线。
- [当前部署档案](../deployments/qwen3.5-9b-rtx5070ti/README.md)：Qwen3.5-9B / RTX 5070 Ti / 128K 身份与机器可读基线。
- [Provider 契约](../contracts/local-qwen-provider-v1.json)：本项目与 ModelPort 的版本化能力边界。
- [ARCHITECTURE.md](ARCHITECTURE.md)：目标、边界、架构和容量设计。
- [OPTIMIZATION.md](OPTIMIZATION.md)：优化参数、A/B 数据、适用边界和后续候选。
- [ENHANCEMENT_ROADMAP.md](ENHANCEMENT_ROADMAP.md)：推理增强、Tool Use Reliability v2、指标口径和分层实施顺序。
- [DEPLOYMENT.md](DEPLOYMENT.md)：模型、镜像、启动和 ModelPort 上线步骤。
- [MODELPORT.md](MODELPORT.md)：ModelPort 路由配置和调用方式。
- [OPERATIONS.md](OPERATIONS.md)：日常启停、监控、升级和故障处理。
- [MAINTENANCE.md](MAINTENANCE.md)：长期运营、实际调用观测、告警阈值和调优闭环。
- [RUNTIME_DASHBOARD.md](RUNTIME_DASHBOARD.md)：本地模型运行台、字段口径和维护方式。
- [ACCEPTANCE.md](ACCEPTANCE.md)：验收范围、命令和通过标准。
- [QUALITY_AND_RELEASE.md](QUALITY_AND_RELEASE.md)：合成质量集、候选端口、晋级与回滚。
- [CACHE.md](CACHE.md)：Prompt RAM Cache 与敏感 KV 快照的使用边界。
- [DECISIONS.md](DECISIONS.md)：关键技术决策及后续演进条件。
- [DEPLOYMENT_RECORD.md](DEPLOYMENT_RECORD.md)：当前机器上的实际版本和验收记录。

## 当前结论

- 硬件：RTX 5070 Ti 16GB、Ryzen 7 9800X3D、96GB 物理内存。
- 运行环境：Ubuntu 24.04 / WSL2；WSL 当前可见约 70GiB RAM。
- 推理：llama.cpp CUDA 容器、Q5_K_M 权重、Q8_0 KV Cache、8GiB 精确前缀缓存。
- 服务：单 Slot、128K 总上下文、默认最多生成 32K tokens。
- Reasoning：默认开启；ModelPort 提供 fast/code/deep 三档预算；生产建议将渲染后输入控制在约 92K。
- Sampling：code 使用精确编码参数，fast/deep 使用通用思考参数，显式请求值优先。
- Token 预算：ModelPort 提供与 llama.cpp/Qwen tokenizer 一致的精确 Count Tokens 接口。
- 上下文准入：总预算超过 128K 或思考输入超过约 92K 时 fail-closed，绝不静默截断。
- 质量与发布：合成质量门禁、独立 `18081` 候选实例和失败自动恢复均已提供。
- Tool Use：ModelPort 完整 JSON Schema 校验；本项目 40 Case 闭环门禁已通过 40/40。
- 延迟：流式 TTFT 取首个可交付正文/工具事件；非流式只报告完整生命周期延迟。
- 观测历史：实时数据写入分层 SQLite 聚合，24 小时原始、30 天分钟、1 年小时保留。
- 接入：ModelPort 的 `local_qwen` provider，通过稳定的 `qwen-runtime` 网络端点访问。
- 默认入口：应用访问 ModelPort，不直接依赖 llama.cpp。
- 端口：Qwen 直连诊断 `127.0.0.1:18080`；ModelPort `127.0.0.1:38082`。
- 已淘汰：当前后端上的 q4 KV、非精确 cache reuse 和 MTP 默认启用，具体 A/B 见优化文档。
- Profile：默认 `latency` 为单 Slot 128K；可选 `throughput` 为双 Slot 64K，双请求聚合吞吐约提升 67.9%。
- 下一阶段：优先实现有边界协议修复、多步/错误恢复、验收 traffic class 和验证器驱动的自适应推理；Q6_K、MTP 和原生构建继续作为候选 A/B，不直接进入生产。

## 优化落点速查

| 改动 | 所属位置 |
| --- | --- |
| 权重、量化、KV、上下文、llama.cpp、缓存和性能候选 | 本项目 |
| Tool Schema、协议修复、工作流状态、Token 准入和逻辑 Profile | ModelPort |
| 闭环合成验收、发布门禁与本地运行台 | 本项目，消费 ModelPort 聚合数据 |
| 业务 Tool 执行、审批、沙箱、幂等和验证器驱动升级 | 应用 / Agent |

跨层设计见 [PROJECT.md](PROJECT.md)，完整增强顺序见
[ENHANCEMENT_ROADMAP.md](ENHANCEMENT_ROADMAP.md)，ModelPort 具体协议边界见
[MODELPORT.md](MODELPORT.md)。

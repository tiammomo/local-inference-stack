# Local Inference Stack

本地模型推理与运营栈 · A production-minded local inference and operations stack.

## 中文介绍

Local Inference Stack 面向单机消费级 GPU，把本地模型制品转化为可重复部署、
可度量、可验收、可回滚并可长期维护的推理服务。它负责模型完整性、llama.cpp
运行时、GPU/KV/上下文 Profile、性能基线、质量回归、ModelPort 能力契约和
隐私友好的实时运行台。

这不是一个“启动模型的脚本集合”，而是一套小型本地 AI 基础设施：版本化部署
清单防止运行态漂移，精确 Token 准入避免静默截断，合成质量集与长上下文召回
阻止性能优化损害效果，独立候选端口和自动恢复流程降低升级风险。

当前生效部署：

- 模型：Qwen3.5-9B GGUF Q5_K_M
- GPU：NVIDIA GeForce RTX 5070 Ti 16GB
- 上下文：128K 单 Slot；默认开启思考，生产建议输入约 92K
- KV Cache：Q8_0 / Q8_0
- 应用入口：ModelPort Anthropic Messages API
- 推理后端：llama.cpp OpenAI-compatible API
- Tool Use：完整 JSON Schema 严格校验、语义终态观测与 40 Case 闭环门禁

```text
应用 / Agent
    │ Anthropic Messages
    ▼
ModelPort :38082
    │ OpenAI-compatible
    ▼
llama.cpp / Qwen :18080
    │ metrics + slots
    ▼
Local Inference Dashboard :33004
```

唯一项目根目录：

```bash
cd /home/tiammomo/projects/infra/local-inference-stack
```

首次安装需要 Docker + NVIDIA Container Toolkit、Python 3.12+，以及已运行的
ModelPort Docker network。仓库不包含约 14GB 的 GGUF 制品；请按
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 下载并校验，再启动服务。

常用命令：

```bash
# 查看运行态
./scripts/runtime.sh status

# 日常、协议和完整验收
./scripts/acceptance-suite.sh quick
./scripts/acceptance-suite.sh standard
./scripts/acceptance-suite.sh full

# 核对运行态与部署清单是否一致
./scripts/verify-deployment.py

# 合成质量冒烟 / 三次重复质量回归
./scripts/quality-eval.py --smoke
./scripts/quality-eval.py --trials 3

# 闭环 Tool Use：5 Case 冒烟 / 40 Case 全量
./scripts/tool-workflow-eval.py --smoke
./scripts/tool-workflow-eval.py

# latency: 1 × 128K；throughput: 2 × 64K
./scripts/runtime.sh profile latency
./scripts/runtime.sh profile throughput
```

模型文件、缓存、日志和凭证不会进入 Git。应用默认只访问 ModelPort；`18080`
仅用于本机诊断和验收。

运行台位于 `http://127.0.0.1:33004`，通过 WebSocket 推送 2 秒瞬时指标和 5 秒
调用聚合；仅保存不含 Prompt、回复和工具参数的分层聚合历史。

## English

Local Inference Stack turns local model artifacts into a reproducible,
observable, testable, and rollback-friendly inference service for a single
workstation GPU. It owns model integrity, llama.cpp runtime profiles,
performance baselines, quality gates, the ModelPort provider contract, and a
privacy-preserving real-time operations dashboard.

The active deployment is Qwen3.5-9B Q5_K_M on an RTX 5070 Ti with one 128K
slot, Q8_0 KV cache, request-level reasoning budgets, complete Tool JSON Schema
validation, semantic Tool outcome telemetry, and a 40-case closed-loop gate.
Applications use the Anthropic Messages edge; the runtime remains an internal
OpenAI-compatible implementation detail.

Prerequisites are Docker with the NVIDIA Container Toolkit, Python 3.12+, and
an active ModelPort Docker network. GGUF artifacts are intentionally excluded;
follow the pinned download and SHA256 instructions in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) before starting the runtime.

## Project boundaries

This repository owns model artifacts, runtime configuration, deployment
manifests, acceptance tests, benchmarks, and aggregate operations data. ModelPort
owns authentication, routing, quotas, accounting, public API protocols, and
reusable Tool Use adaptation. Applications own prompts, RAG, agents, and
business tools.

优化实现遵循同一边界：

| 层次 | 后续优化落点 |
| --- | --- |
| 本项目 | Q5/Q6、KV、128K、llama.cpp、缓存、性能 A/B、闭环质量集、候选发布和运行台 |
| ModelPort | Tool JSON Schema 校验、流式协议修复、工作流状态、Token 准入、逻辑 Profile 和网关指标 |
| 应用 / Agent | Tool Registry、实际执行、权限审批、沙箱、幂等，以及基于测试反馈的 `fast -> code -> deep` 升级 |

ModelPort 不执行任意业务工具，本项目也不复制它的协议实现。跨仓库改动先更新稳定
Provider 契约，再由本项目通过真实上游验收。

The same ownership applies to optimization work: this repository owns runtime
and evaluation, ModelPort owns reusable protocol behavior and telemetry, and
applications own tool execution, safety, and verifier-driven escalation.

The retired `/home/tiammomo/projects/infra/models` path must not be recreated or
used by scripts, services, or containers.

## Documentation

- [Project definition](docs/PROJECT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Active deployment](deployments/qwen3.5-9b-rtx5070ti/README.md)
- [Optimization evidence](docs/OPTIMIZATION.md)
- [Inference and Tool Use enhancement roadmap](docs/ENHANCEMENT_ROADMAP.md)
- [Operations and maintenance](docs/MAINTENANCE.md)
- [Acceptance criteria](docs/ACCEPTANCE.md)
- [Quality gates and release workflow](docs/QUALITY_AND_RELEASE.md)
- [Prompt and KV cache operations](docs/CACHE.md)
- [ModelPort integration](docs/MODELPORT.md)
- [Provider contract](contracts/local-qwen-provider-v1.json)

## Security and privacy

All services listen on loopback interfaces. Operations reports retain aggregate
health, latency, token, cache, and Tool Use outcome signals only; prompts,
responses, tool names, tool arguments, request IDs, identities, API keys, and
client IP addresses are excluded.

## Current engineering guarantees

- Exact Anthropic token counting and fail-closed 128K context admission.
- Reasoning-aware 92K production input ceiling with explicit opt-out for
  non-reasoning capacity tests.
- Fail-closed Tool Use framing validation and request-level outcome telemetry;
  strict mode validates complete per-tool JSON Schema before non-stream return
  or a stream's successful `content_block_stop` terminal signal.
- A 40-case closed-loop Tool Use suite executes deterministic mock tools,
  returns `tool_result`, and verifies the model's final answer without retaining
  prompts, arguments, results, or model output in evidence.
- Stream-only TTFT measures the first deliverable text/tool event; non-stream
  latency is never relabeled as TTFT.
- WebSocket live operations data plus bounded SQLite aggregate retention.
- Synthetic quality gates, recorded acceptance evidence, and serial release
  candidates on port `18081` with automatic production recovery.

## 下一阶段 / Next engineering phase

当前运行时参数已经形成稳定基线。下一阶段优先提升可验证的任务完成率，而不是无证据
地继续增加启动参数：

1. 在已完成完整 JSON Schema 校验和请求级 Tool 决策观测的基础上，增加一次有边界
   的协议修复；实际工具执行、授权与沙箱仍由应用负责。
2. 将当前 40 Case 两轮闭环集继续扩展到多步调用、错误恢复、流式分片和 Prompt
   Injection，并建立连续多次运行的稳定门槛。
3. 为验收调用增加独立 traffic class，把业务 SLO 与合成流量彻底分开。
4. 使用验证器驱动的 `fast -> code -> deep` 自适应升级，并以编译器、测试和工具
   反馈增强代码与 Agent 任务。
5. 保留 128K 容量，但通过上下文压缩和检索把日常工作集尽量控制在 32K 内。

The next phase builds on schema-complete Tool Use, closed-loop evaluation, and
stream-only TTFT with bounded repair, multi-step/error-recovery cases, explicit
test traffic classification, and verifier-driven adaptive escalation. Runtime,
gateway, and application responsibilities remain deliberately separated. See the
[enhancement roadmap](docs/ENHANCEMENT_ROADMAP.md) for priorities and release
gates.

# Local Inference Stack

让 Agent 在一台新机器上完成“检测硬件 → 推荐模型 → 安全下载 → 本地部署 →
验收”的可复现推理栈。Clone it, let an Agent assess the host, then deploy a
reviewed local model with pinned artifacts and repeatable acceptance.

## 60 秒了解它

本项目不是某一台机器的启动脚本，也不包含模型权重。它提供：

- 只读硬件探测和机器可读推荐结果；
- 面向 2–32GB NVIDIA 显存的 Qwen3.5 GGUF 候选目录；
- 固定 URL、字节数和 SHA256 的可续传下载；
- 经过安全收敛的 llama.cpp CUDA Compose 运行时；
- 思考模式、长上下文、质量、性能和 Tool Use 验收；
- 可选的 ModelPort Anthropic Messages 接入与实时运行台。

当前唯一实机验证基线是 **RTX 5070 Ti 16GB + Qwen3.5-9B Q5_K_M +
128K 单 Slot + Q8_0 KV**。其他档位是保守的启动估算，必须在目标机器通过
`quick` 验收后才能升级为已验证部署。

## 首次使用

要求 Linux/WSL x86_64、NVIDIA 驱动、Docker Compose v2 和已配置的 NVIDIA
Container Toolkit。安装方式以 [NVIDIA 官方指南](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
为准。

```bash
git clone git@github.com:tiammomo/local-inference-stack.git
cd local-inference-stack

# 只读：不下载、不启动、不修改配置
./scripts/model-manager.py plan
./scripts/model-manager.py plan --json   # 推荐给 Agent 使用
```

确认输出中的模型状态、大小、来源、许可证和硬件余量后，才执行有副作用的步骤：

```bash
MODEL_ID=qwen35-9b-q5km  # 使用 plan 实际给出的 id
./scripts/model-manager.py download --model "$MODEL_ID" --yes
./scripts/model-manager.py select --model "$MODEL_ID" --yes
./scripts/runtime.sh start latency
./scripts/acceptance-suite.sh quick
```

`download` 会先写入 `.part`，校验精确大小和 SHA256 后才原子替换为正式权重。
模型、缓存、日志、生成 Profile 和凭证均不会进入 Git。

## 硬件推荐档位

| 最低显存 | 默认候选 | 量化 | 初始上下文 | 状态 |
| ---: | --- | --- | ---: | --- |
| 2GB | Qwen3.5-0.8B | Q5_K_M | 32K | 估算 |
| 4GB | Qwen3.5-2B | Q5_K_M | 32K | 估算 |
| 6GB | Qwen3.5-4B | Q5_K_M | 64K | 估算 |
| 10GB | Qwen3.5-9B | Q4_K_M | 64K | 估算 |
| 14GB | Qwen3.5-9B | Q5_K_M | 128K | **5070 Ti 已验证** |
| 22GB | Qwen3.5-27B | Q4_K_M | 32K | 估算 |
| 28GB | Qwen3.5-35B-A3B | Q4_K_M | 32K | 估算 |

推荐同时检查 RAM 和磁盘，不只看显存。多 GPU、CPU、Apple Silicon、AMD 和共享
GPU 暂不自动部署；详见 [硬件与模型选择](docs/HARDWARE_GUIDE.md)。完整数据以
[`catalog/models.json`](catalog/models.json) 为准，表格不是独立配置源。

## 使用接口

独立部署默认提供本机 OpenAI-compatible API：

```bash
curl --noproxy '*' http://127.0.0.1:18080/v1/models
curl --noproxy '*' http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-9b-q5km","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'
```

应用级长期接入建议使用 ModelPort 的 Anthropic Messages 边界；直接端口只用于本机
诊断。ModelPort 不是首次部署的前置条件，接入方式见 [ModelPort 集成](docs/MODELPORT.md)。

常用运行命令：

```bash
./scripts/runtime.sh status
./scripts/runtime.sh logs
./scripts/runtime.sh profile latency       # 单 Slot，优先延迟与长上下文
./scripts/runtime.sh profile throughput    # 双 Slot，优先并发
./scripts/model-manager.py verify --cached
./scripts/install-user-services.py --enable
```

## Agent 操作契约

[`AGENTS.md`](AGENTS.md) 是克隆后 Agent 的入口：默认只能运行只读 `plan --json`；
必须先向用户展示下载大小、来源和候选状态，获得明确批准后才能使用 `--yes`。任何
未列入 Catalog 或未经目标机验收的配置，都不能声称“已验证”。

## English quick start

This repository provides a safe, catalog-backed path from a fresh NVIDIA host
to a local llama.cpp service. Start with the read-only JSON plan:

```bash
./scripts/model-manager.py plan --json
```

Review the recommendation, artifact publisher, license, size, SHA256 policy,
and `validated` versus `estimated` status. Only then approve `download` and
`select` with `--yes`, start the runtime, and run the quick acceptance suite.
The standalone edge is OpenAI-compatible on loopback; ModelPort is an optional
Anthropic-compatible production gateway.

Automation is deliberately limited to Linux/WSL x86_64 NVIDIA CUDA hosts. The
hardware thresholds include VRAM, system RAM, free disk, model weights, KV
cache, and runtime headroom, but all entries except the recorded RTX 5070 Ti
baseline remain estimates until tested on the target machine.

## 文档 / Documentation

- [首次部署闭环](docs/GETTING_STARTED.md)
- [硬件与模型选择](docs/HARDWARE_GUIDE.md)
- [项目边界](docs/PROJECT.md)
- [架构](docs/ARCHITECTURE.md)
- [验收](docs/ACCEPTANCE.md)
- [安全与发布检查](docs/SECURITY_AND_RELEASE.md)
- [当前 5070 Ti 部署档案](deployments/qwen3.5-9b-rtx5070ti/README.md)
- [优化证据](docs/OPTIMIZATION.md)
- [推理与 Tool Use 路线](docs/ENHANCEMENT_ROADMAP.md)

## 安全边界

服务默认只绑定 `127.0.0.1`，容器使用只读根文件系统、非 root 用户、删除全部
capabilities，并固定 llama.cpp 镜像 digest。Catalog 中的哈希只能证明下载内容与
审查对象一致，不能替代对模型、GGUF 发布者和许可证的信任审查。不要把 `18080`
或运行台直接暴露到局域网或公网。

安全问题请按 [`SECURITY.md`](SECURITY.md) 使用 GitHub 私密漏洞报告；不要在公开
Issue 中提交凭证、Prompt、回复、工具参数或原始日志。本仓库代码当前未声明开源
许可证，公开可见不等于授予再分发权；模型与 GGUF 许可证还需分别审查。

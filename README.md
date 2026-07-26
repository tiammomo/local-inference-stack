# Local Inference Stack

面向 Linux/WSL NVIDIA 单机的本地 LLM 推理栈：硬件评估、Catalog 选型、可校验
GGUF 下载、llama.cpp CUDA 运行、验收、发布和运维。模型权重不进入 Git。

当前唯一实机验证档位是 RTX 5070 Ti 16GB、Qwen3.5-9B Q5_K_M、128K 单 Slot、
Q8_0 KV。其他 Catalog 条目是保守估算，必须在目标主机重新验收。

## 快速开始

要求：Linux/WSL x86_64、NVIDIA GPU/驱动、NVIDIA Container Toolkit、
Docker Compose v2、Python 3.10+ 和 `curl`。

```bash
git clone git@github.com:tiammomo/local-inference-stack.git
cd local-inference-stack

# 唯一默认动作：只读，不下载、不启动、不改配置
./scripts/model-manager.py plan --json
```

先检查推荐模型、`evidenceStatus`、`hostAcceptanceStatus`、下载大小、固定 revision、
许可证、SHA256、空闲显存和 `caveats`。只有 `readyToDeploy=true` 且
`nextCommands` 非空，并得到用户明确批准后，才执行：

```bash
MODEL_ID=qwen35-9b-q5km # 替换为 plan 返回的 id
./scripts/model-manager.py download --model "$MODEL_ID" --yes
./scripts/model-manager.py select --model "$MODEL_ID" --yes
./scripts/runtime.sh start latency
./scripts/acceptance-suite.sh quick
```

下载支持断点续传，只有精确字节数和 SHA256 匹配才会发布。`quick` 验证权重、
运行态、生成和思考，并生成与本机及当前配置绑定的限时验收凭证。

## 使用与运维

直连诊断 API 只监听 `127.0.0.1:18080`：

```bash
curl --noproxy '*' http://127.0.0.1:18080/v1/models
curl --noproxy '*' http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-9b-q5km","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'
```

常用命令：

```bash
./scripts/runtime.sh status
./scripts/runtime.sh logs
./scripts/model-manager.py verify --cached
./scripts/acceptance-suite.sh quick
./scripts/install-user-services.py --enable
```

应用长期接入可选 ModelPort Anthropic Messages 网关；业务工具的执行、审批、沙箱和
幂等不属于本仓库。

## 文档

- [首次部署](docs/GETTING_STARTED.md)
- [硬件与模型](docs/HARDWARE_GUIDE.md)
- [架构与边界](docs/ARCHITECTURE.md)
- [ModelPort 接入](docs/MODELPORT.md)
- [验收](docs/ACCEPTANCE.md)
- [运维与恢复](docs/OPERATIONS.md)
- [当前验证档案](deployments/qwen3.5-9b-rtx5070ti/README.md)

自动化操作约束见 [AGENTS.md](AGENTS.md)。

## 安全边界

- 仅支持可信单用户主机；不要把 `18080`、ModelPort 或运行台直接暴露到网络。
- 多 GPU、CPU、Apple Silicon、AMD、共享生产 GPU 均需人工设计独立 Profile。
- 固定哈希只证明制品身份，不替代模型、GGUF 发布者和许可证审查。
- 模型、缓存、日志、备份、本地 Profile 和凭证均应保持 Git 忽略。

安全问题使用 GitHub 私密漏洞报告，详见 [SECURITY.md](SECURITY.md)。

## License

仓库自产内容采用 [MIT License](LICENSE)；模型、GGUF、镜像和其他第三方组件遵循
各自许可证。

# Qwen3.5-9B / RTX 5070 Ti / 128K

这是 Local Inference Stack 当前生效的部署档案。

| 项目 | 生效值 |
| --- | --- |
| 模型 | Qwen3.5-9B GGUF |
| 权重量化 | Q5_K_M |
| KV Cache | Q8_0 / Q8_0 |
| 总上下文 | 131,072，单 Slot |
| 生产思考输入 | 约 92K rendered tokens |
| 最大输出 | 32,768 tokens |
| GPU | NVIDIA GeForce RTX 5070 Ti 16GB |
| 运行时 | llama.cpp CUDA，固定 OCI digest |
| 网关 | ModelPort `local_qwen` |
| 应用协议 | Anthropic Messages |
| 运行时协议 | OpenAI-compatible Chat Completions |

机器可读基线见 [manifest.json](manifest.json)。完整实测与 SHA256 见
[部署记录](../../docs/DEPLOYMENT_RECORD.md)，参数依据见
[优化文档](../../docs/OPTIMIZATION.md)。

此目录描述部署身份；可执行配置仍由根目录 `compose.yaml`、`profiles/` 和
`scripts/` 维护，避免同一配置出现两份可执行来源。

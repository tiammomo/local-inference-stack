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

Manifest 同时固定官方模型 revision、GGUF 发布 revision、许可证元数据、权重字节数/
SHA256，以及运行、候选、发布和验证控制脚本。`validated` 表示该硬件档位存在可复查
基线；新主机仍须生成自己的 acceptance 证据。

本机 schema v3 acceptance 证据还绑定应用域机器指纹、当前驱动、镜像、关键配置和
quick 传递依赖，并要求安全文件属性、自校验正确且不超过 30 天；跨主机复制、过期或
任一字段漂移后，`plan` 会自动撤销 `validated-on-this-host`，直到重新通过验收。
当前记录 `logs/acceptance/20260726T121722Z-quick.json` 已通过 34 项单元测试、
权重、生成和默认思考复验；迁移时旧 v2 凭证先被 fail-closed 拒绝，生产容器未重启
或重建。

ModelPort commit、镜像来源标签与 Provider 配置的联合发布流程见
[跨仓库发布契约](../../docs/CROSS_REPOSITORY_RELEASE.md)。

此目录描述部署身份；可执行配置仍由根目录 `compose.yaml`、`profiles/` 和
`scripts/` 维护，避免同一配置出现两份可执行来源。

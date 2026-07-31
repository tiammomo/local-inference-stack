# Qwen3.5-9B / RTX 5070 Ti

当前唯一实机验证档案。机器可读事实以 [manifest.json](manifest.json) 为准。

| 项目 | 基线 |
| --- | --- |
| GPU / RAM | RTX 5070 Ti 16GB / 96GB |
| 模型 | Qwen3.5-9B GGUF Q5_K_M |
| 运行时 | 固定 digest 的 llama.cpp CUDA |
| 上下文 | 128K，单 Slot |
| KV / Cache | Q8_0 K/V，8GiB Prompt Cache |
| 思考预算 | 建议输入约 92K，最多输出 32,768 |
| 接口 | `127.0.0.1:18080`；可选 ModelPort `38082` |

已验证直连生成、思考、118K 召回、92K ModelPort 链路、Tool Use、质量、备份恢复、
串行候选和部署漂移检查。典型短解码约 88–90 tok/s，峰值显存低于 12.4GiB；
这些数据是本机基线，不是其他硬件的承诺。

未采用：q4 KV 会显著拖慢长提示预填充；MTP 在长上下文回退；`batch=4096` 无收益；
双 Slot 只作为牺牲单请求速度和上下文的 `throughput` Profile。

```bash
./scripts/model-manager.py plan --json
./scripts/acceptance-suite.sh quick
python3 ./scripts/verify-deployment.py
```

新主机、驱动、镜像、模型或配置变化后必须重新验收；不能复制本机凭证来继承
`validated-on-this-host`。

## 供应链与许可证审查（2026-08-01）

- 上游模型固定为 `Qwen/Qwen3.5-9B@c202236235762e1c871ad0ccb60c8ee5ba337b9a`；
  仓库元数据和固定 revision 的许可证文件标记 Apache-2.0：
  <https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/LICENSE>。
- GGUF 来自第三方发布者 Unsloth，固定 revision
  `3885219b6810b007914f3a7950a8d1b469d598a5`。该 revision 列出
  `Qwen3.5-9B-Q5_K_M.gguf`，本机文件为 6,577,841,376 bytes、单硬链接、`0600`，并重新计算
  SHA256 为 `dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c`：
  <https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/3885219b6810b007914f3a7950a8d1b469d598a5>。
- llama.cpp CUDA 镜像固定 digest
  `sha256:0d6c600a69e8bdaafd7b91ed6db9160906ee8148ee12a609cf4d52b4e17aabe8`。
  本地 OCI 标签把镜像关联到官方源码 commit
  `12127defda4f41b7679cb2477a4b0d65ee6a0c8f`（build `b10015`）；该 revision 的源码许可证为 MIT：
  <https://github.com/ggml-org/llama.cpp/commit/12127defda4f41b7679cb2477a4b0d65ee6a0c8f>、
  <https://github.com/ggml-org/llama.cpp/blob/12127defda4f41b7679cb2477a4b0d65ee6a0c8f/LICENSE>。

结论：这些固定来源可接受用于可信单用户、loopback-only 的本机部署。风险仍包括第三方 GGUF
量化过程未在本仓库复现、容器的 CUDA/Ubuntu 传递依赖，以及许可证适用性的组织/法律判断；
因此 `licenseReviewRequired` 保持为 true。任何模型 revision、GGUF 哈希或镜像 digest 变化都必须
重新审查和验收，不能自动追踪 latest。

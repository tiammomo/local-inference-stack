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

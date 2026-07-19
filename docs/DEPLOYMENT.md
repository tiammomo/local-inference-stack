# 部署步骤

## 固定制品

| 制品 | 固定值 |
| --- | --- |
| 模型 | `unsloth/Qwen3.5-9B-GGUF` |
| 权重 | `Qwen3.5-9B-Q5_K_M.gguf` |
| 权重 SHA256 | `dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c` |
| MTP A/B 制品 | `Qwen3.5-9B-MTP-Q5_K_M.gguf`，生产不加载 |
| MTP 制品 SHA256 | `1732d6616554b102be9bc41684cd094f471e1b3067f5e5a89eb5a86a5a4f2a6c` |
| 视觉投影器 | `mmproj-BF16.gguf`，基线不加载 |
| 投影器 SHA256 | `853698ce7aa6c7ba732478bad280240969ddf7b0fcbf93900046f63903a83383` |
| llama.cpp 镜像 | `ghcr.io/ggml-org/llama.cpp:server-cuda` |
| OCI index digest | `sha256:0d6c600a69e8bdaafd7b91ed6db9160906ee8148ee12a609cf4d52b4e17aabe8` |

运行基线为 `ctx-size=131072`、`parallel=1`、Q8_0 K/V Cache、
Flash Attention、全部模型层 GPU offload、`batch-size=2048`、
`ubatch-size=1024`、8GiB 提示缓存和 `ngram-mod` 推测解码，并通过
`--reasoning on` 默认开启思考。预算耗尽时会注入收束提示，服务端生成上限为
32,768 tokens。

## 目录

```text
infra/local-inference-stack/
├── compose.yaml
├── models/qwen3.5-9b/
├── cache/
├── logs/
├── profiles/
├── scripts/
└── docs/
```

## 下载

```bash
cd /home/tiammomo/projects/infra/local-inference-stack

curl -fL --retry 8 --retry-all-errors --continue-at - \
  --output models/qwen3.5-9b/Qwen3.5-9B-Q5_K_M.gguf \
  'https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q5_K_M.gguf?download=true'

curl -fL --retry 8 --retry-all-errors --continue-at - \
  --output models/qwen3.5-9b/mmproj-BF16.gguf \
  'https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-BF16.gguf?download=true'

curl -fL --retry 8 --retry-all-errors --continue-at - \
  --output models/qwen3.5-9b/Qwen3.5-9B-MTP-Q5_K_M.gguf \
  'https://huggingface.co/unsloth/Qwen3.5-9B-MTP-GGUF/resolve/main/Qwen3.5-9B-Q5_K_M.gguf?download=true'

scripts/verify-models.sh
```

## 启动

ModelPort 的 Compose 网络必须存在：

```bash
docker network inspect modelport_default
scripts/runtime.sh start
scripts/runtime.sh status
```

启用登录/启动后的幂等恢复：

```bash
mkdir -p ~/.config/systemd/user
ln -sf /home/tiammomo/projects/infra/local-inference-stack/deploy/systemd/qwen-model-runtime.service \
  ~/.config/systemd/user/qwen-model-runtime.service
systemctl --user daemon-reload
systemctl --user enable --now qwen-model-runtime.service
```

等 `/health` 返回 `{"status":"ok"}` 后执行：

```bash
scripts/smoke-test.sh
```

`/props` 的 `default_generation_settings.n_ctx` 和 `/slots` 的 `n_ctx`
都必须为 `131072`。

## ModelPort 上线

`/home/tiammomo/projects/dev/ModelPort/.env` 只保存部署环境值：

```env
MODELPORT_DEFAULT_PROVIDER=local_qwen
QWEN_LOCAL_BASE_URL=http://qwen-runtime:8080/v1
```

provider 展示名、模型 ID、Tool Use 能力和内部费率卡
`local-qwen-2026q3-v1` 在 ModelPort 的 `config.toml` 中声明；模型运行参数仍只在
本目录的 `compose.yaml` 中声明。具体费率与职责边界见 `docs/MODELPORT.md`。

默认启动单 Slot 128K：

```bash
scripts/runtime.sh start latency
```

需要两路 Agent 聚合吞吐时可切换双 Slot，每 Slot 64K；该模式不适合 92K/118K
验收，使用完恢复默认：

```bash
scripts/runtime.sh profile throughput
scripts/runtime.sh profile latency
```

重新创建服务，确保 Docker Desktop 不复用失效的 WSL bind mount：

```bash
cd /home/tiammomo/projects/dev/ModelPort
docker compose rm -sf modelport dashboard
docker compose up -d --build modelport dashboard
docker compose ps
```

然后回到模型目录运行端到端测试：

```bash
cd /home/tiammomo/projects/infra/local-inference-stack
scripts/modelport-smoke.sh
```

升级镜像或运行参数时先使用 `scripts/release-candidate.sh quick` 在独立 `18081` 端口
串行验收，不能直接覆盖生产实例。128K 运行时已启用本地 KV 快照目录，但不会自动
保存或恢复真实会话；边界见 `CACHE.md`。

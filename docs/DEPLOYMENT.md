# 部署步骤

通用首次部署入口见 [GETTING_STARTED.md](GETTING_STARTED.md)。本页记录运行时如何把
Catalog 选择物化为 Compose 服务，以及当前 9B 基线的高级操作。

## Catalog 驱动部署

```bash
./scripts/model-manager.py plan --json

# 用户审阅并明确批准后：
MODEL_ID=qwen35-9b-q5km
./scripts/model-manager.py download --model "$MODEL_ID" --yes
./scripts/model-manager.py select --model "$MODEL_ID" --yes
./scripts/runtime.sh start latency
./scripts/acceptance-suite.sh quick
```

`catalog/models.json` 是模型 URL、文件名、精确字节数、SHA256、硬件门槛和默认运行
参数的唯一配置源。不要把网页中的“latest”链接或未经固定的下载脚本加到部署流程。

## 运行物化

`select` 生成本地 `profiles/deployment.local.env`：

- 模型目录、文件和对外 model ID；
- 容器名；
- 上下文、最大生成、Prompt RAM Cache；
- batch/ubatch。

该文件权限为 `0600` 且被 Git 忽略。Compose 再叠加 `latency.env` 或
`throughput.env`，后加载的部署 Profile 保留 Catalog 的安全容量参数。服务默认使用：

- 固定 digest 的 llama.cpp CUDA server；
- 全层 GPU offload、Flash Attention；
- Q8_0 K/V Cache；
- Jinja 与默认思考模式；
- 只读根文件系统、非 root、无 Linux capabilities；
- `127.0.0.1:18080` 诊断端口。

启动会创建缺失的 `${MODELPORT_NETWORK_NAME:-modelport_default}` Docker 网络。该网络
只是稳定的可选集成面；ModelPort 不再是本地模型启动的前置条件。

## 当前验证制品

RTX 5070 Ti 档案仍固定以下基线：

| 制品 | 固定值 |
| --- | --- |
| 模型仓库 | `unsloth/Qwen3.5-9B-GGUF` |
| 权重 | `Qwen3.5-9B-Q5_K_M.gguf` |
| 权重 SHA256 | `dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c` |
| llama.cpp OCI digest | `sha256:0d6c600a69e8bdaafd7b91ed6db9160906ee8148ee12a609cf4d52b4e17aabe8` |
| 运行时 | 128K、单 Slot、Q8_0 K/V、32K 最大生成、思考开启 |

MTP 和视觉投影器不参与文本基线，也不在通用 Catalog 中自动下载。历史 A/B 的固定
制品与拒绝结论保留在 [OPTIMIZATION.md](OPTIMIZATION.md)；重新开启实验必须单独做
来源、扫描、显存、性能和质量审查，不能借用首次部署命令。

## systemd 用户服务

仓库保存可迁移模板，不保存任何用户绝对路径：

```bash
./scripts/install-user-services.py --enable
```

运营台和日报属于可选 ModelPort 集成，需要先最小化复制本地凭证：

```bash
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
./scripts/install-user-services.py --operations --enable
```

## ModelPort 上线

当前版本化契约只验证 `qwen35-9b-q5km`。ModelPort 环境中的稳定上游为：

```env
MODELPORT_DEFAULT_PROVIDER=local_qwen
QWEN_LOCAL_BASE_URL=http://qwen-runtime:8080/v1
```

应用使用 ModelPort Anthropic Messages API，ModelPort 使用 OpenAI-compatible 上游。
切换 Catalog 模型前必须协调模型 ID、上下文、Token 准入和能力契约。测试命令：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
```

升级镜像、权重或关键运行参数时仍应通过独立候选端口与回滚流程，不能直接覆盖已经
验证的生产实例。细节见 [QUALITY_AND_RELEASE.md](QUALITY_AND_RELEASE.md)。

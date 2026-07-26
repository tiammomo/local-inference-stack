# 首次部署

标准路径是“只读规划 → 人工批准 → 下载/选择 → 启动 → quick 验收”。不要跳过规划，
也不要手写 Catalog 外的 URL、文件名或哈希。

## 1. 前置条件

- Linux/WSL x86_64 NVIDIA 主机；
- NVIDIA 驱动与 Container Toolkit；
- Docker Engine、Docker Compose v2；
- Python 3.11+、`curl` 和足够磁盘。

```bash
nvidia-smi
docker info
docker compose version
```

本仓库不安装驱动、Docker 或系统组件。

## 2. 只读规划

```bash
./scripts/model-manager.py plan --json
```

必须审阅：

- 推荐模型、Catalog 状态与许可证；
- 固定的模型/GGUF revision、字节数和 SHA256；
- `evidenceStatus` 与 `hostAcceptanceStatus`；
- VRAM/RAM/磁盘、当前空闲显存和 `caveats`；
- `readyToDeploy` 与 `nextCommands`。

`recommendation=null`、`readyToDeploy=false` 或空 `nextCommands` 都是停止条件。硬件档位
相似不等于本机已经验收。

## 3. 批准后部署

```bash
MODEL_ID=qwen35-9b-q5km # 替换为 plan 返回的 id
./scripts/model-manager.py download --model "$MODEL_ID" --yes
./scripts/model-manager.py select --model "$MODEL_ID" --yes
./scripts/model-manager.py verify --cached
./scripts/runtime.sh start latency
./scripts/runtime.sh status
./scripts/acceptance-suite.sh quick
```

下载保留可续传 `.part`；只有精确大小和 SHA256 匹配才会原子发布。`select` 只写入
Git 忽略、权限 `0600` 的本地 Profile。`quick` 通过后生成 schema v3 本机验收凭证；
主机、驱动、制品、镜像、关键脚本、权限或时间发生漂移时凭证自动失效。

默认 API 为 `http://127.0.0.1:18080/v1`，不得直接暴露到网络。

## 4. 可选能力

开机恢复：

```bash
./scripts/install-user-services.py --enable
```

ModelPort、Dashboard、备份和恢复演练：

```bash
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
cp profiles/backup.local.env.example profiles/backup.local.env
# 在 backup.local.env 设置 MODELPORT_PROJECT_DIR
./scripts/install-user-services.py --operations --enable
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
```

详情见 [ModelPort 接入](MODELPORT.md) 和 [运维与恢复](OPERATIONS.md)。

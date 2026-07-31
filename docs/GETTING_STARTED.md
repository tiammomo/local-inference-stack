# 首次部署

标准路径是“只读规划 → 人工批准 → 下载/选择 → 启动 → quick 验收”。不要跳过规划，
也不要手写 Catalog 外的 URL、文件名或哈希。

## 1. 前置条件

- Linux/WSL x86_64 NVIDIA 主机；
- NVIDIA 驱动与 Container Toolkit；
- Docker Engine、Docker Compose v2；
- uv、Python 3.11+、`curl`、util-linux `flock` 和足够磁盘。当前已验证基线固定
  uv-managed CPython 3.14.6，精确版本见 `.python-version`。
- 联合运行 ModelPort `standard/full` 验收时，还需 Linux Node.js（推荐 Node 24）。

```bash
nvidia-smi
docker info
docker compose version
python3 --version
uv --version
```

本仓库不安装驱动、Docker 或系统组件。

## 2. 只读规划

```bash
./stack plan --json
```

必须审阅：

- 推荐模型、Catalog 状态与许可证；
- 固定的模型/GGUF revision、字节数和 SHA256；
- `evidenceStatus` 与 `hostAcceptanceStatus`；
- VRAM/RAM/磁盘、当前空闲显存和 `caveats`；
- `readyToDeploy` 与 `nextCommands`。

`recommendation=null`、`readyToDeploy=false` 或空 `nextCommands` 都是停止条件。容量覆盖参数
只用于模拟，永远不会授权部署。也可以用
`./scripts/model-manager.py admit --model <catalog-id> --json` 单独重复只读准入。硬件档位
相似不等于本机已经验收。

## 3. 批准后部署

```bash
MODEL_ID=qwen35-9b-q5km # 替换为 plan 返回的 id
./stack deploy --model "$MODEL_ID" --yes
./stack status
```

`deploy` 只执行规划器返回的 Catalog 命令。下载保留可续传 `.part`；只有精确大小和 SHA256
匹配才会原子发布。`select` 只接受已校验的必需制品，并写入 Git 忽略、权限 `0600` 的本地
Profile。运行变更由 `flock` 和持久事务串行化，启动必须
等待健康。`quick` 通过后生成 schema v4 本机验收凭证；主机、驱动、制品、镜像、有效 Compose、
实际容器安全配置、关键脚本、Profile、权限或时间发生漂移时凭证自动失效。

默认 API 为 `http://127.0.0.1:18080/v1`，不得直接暴露到网络。

## 4. 可选能力

开机恢复：

```bash
uv python install "$(cat .python-version)"
./scripts/install-user-services.py --runtime-only --enable
```

runtime-only 安装会收敛并禁用之前遗留的 Dashboard、报表、备份和恢复演练 unit，但不会
删除其日志或备份数据。常驻 supervisor 在 Docker 后端暂不可用时每 60 秒重试，10 分钟后
写入本地私有告警并继续等待；运行成功后每 5 分钟校验健康、固定 Profile 和容器身份。
显存准入、模型哈希或配置漂移失败不会被自动绕过。

ModelPort、Dashboard、备份和恢复演练：

```bash
# 使用 NVM 时，先让非交互验收也能找到 Linux Node.js
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm use 24
node --version

./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
cp profiles/backup.local.env.example profiles/backup.local.env
# 在 backup.local.env 设置 MODELPORT_PROJECT_DIR
./scripts/install-user-services.py --operations --enable
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
```

详情见 [ModelPort 接入](MODELPORT.md) 和 [运维与恢复](OPERATIONS.md)。

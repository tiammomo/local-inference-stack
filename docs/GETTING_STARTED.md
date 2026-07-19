# 首次部署闭环

本页定义新机器和 Agent 的标准路径。唯一默认动作是只读规划；模型下载、Profile
写入、容器启动和 systemd 安装都必须得到用户明确批准。

## 1. 前置条件

- Linux 或 WSL2 x86_64；
- NVIDIA GPU 与可用的 `nvidia-smi`；
- Docker Engine、Docker Compose v2；
- 已按 NVIDIA 官方文档配置 Container Toolkit；
- Python 3.10+、`curl`、足够的本机磁盘；
- 能访问 Hugging Face，或由管理员预先放置并校验 GGUF。

先检查 Docker 能看到 GPU：

```bash
nvidia-smi
docker info
docker compose version
```

本项目不会用提权脚本替你安装驱动、Docker 或 Container Toolkit。系统级安装必须
遵循发行版和 [NVIDIA 官方安装指南](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

## 2. 只读规划

```bash
./scripts/model-manager.py list
./scripts/model-manager.py plan
./scripts/model-manager.py plan --json
```

`plan` 读取 GPU、显存、RAM、磁盘与 Docker 状态，不联网、不写文件、不拉镜像、
不下载权重。Agent 应把 JSON 中以下字段展示给用户：

- `recommendation.id`、`displayName`、`status` 与主机专属的 `evidenceStatus`；
- `requirements` 与 `runtime`；
- 主权重的 `bytes`、`url`、`sha256`；
- `fits`、`caveats` 和 `nextCommands`。

如果 `recommendation` 为 `null`，不要强行部署。当前自动化不覆盖无 NVIDIA GPU、
少于 2GB 显存或异常平台。

## 3. 审批后下载与选择

```bash
MODEL_ID=qwen35-9b-q5km
./scripts/model-manager.py plan --model "$MODEL_ID"
./scripts/model-manager.py download --model "$MODEL_ID" --yes
./scripts/model-manager.py select --model "$MODEL_ID" --yes
./scripts/model-manager.py verify --cached
```

下载只处理 Catalog 中的 HTTPS URL。中断后保留 `.part` 供续传；只有字节数和 SHA256
完全匹配才会原子发布。`select` 仅写入权限为 `0600` 且被 Git 忽略的
`profiles/deployment.local.env`，不包含 Token。

第三方 GGUF 由 Catalog 的 `artifactPublisher` 发布。部署前仍需核对模型作者、制品
发布者、许可证和适用政策；固定哈希不是供应链背书。

## 4. 启动与直连验收

```bash
./scripts/runtime.sh start latency
./scripts/runtime.sh status
./scripts/acceptance-suite.sh quick
```

启动会幂等创建共享 Docker 网络，但不会安装或启动 ModelPort。直连接口固定绑定
`127.0.0.1:18080`。`quick` 包含单元测试、运行态、生成和思考模式，不需要密钥。

如果是 `estimated` 候选，重点观察：

- 是否完整 GPU offload，是否发生 OOM；
- 实际显存余量和首请求峰值；
- 上下文/并发 Profile 是否满足目标；
- 思考内容与最终答案是否正常；
- 至少一次业务代表性质量测试。

通过后可以把主机、GPU、驱动、镜像、权重、Profile 和验收证据记录到新的
`deployments/<model>-<hardware>/`，再提议把状态升级为 `validated`。

## 5. 可选：开机恢复与 ModelPort

```bash
./scripts/install-user-services.py --enable
```

安装器会把仓库中的模板渲染到当前用户的 systemd 目录，因此仓库可以位于任意路径。
运营台还需要本地凭证，不能在首次部署时默认启用：

```bash
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
./scripts/install-user-services.py --operations --enable
```

ModelPort 协议验收只适用于当前版本化的 9B Provider 契约：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
```

切换到其他 Catalog 模型时，先协调更新 ModelPort 的模型映射和 Provider 契约，不能
让网关静默把旧逻辑模型指向一个新模型。

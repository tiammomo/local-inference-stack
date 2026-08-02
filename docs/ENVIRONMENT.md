# 环境与配置来源

本页回答三个问题：项目真正需要什么、配置从哪里来、本机上哪些文件不能进 Git。
这是便携式项目文档，不记录具体用户的绝对路径、凭据值或临时硬件数据。

## 必需与可选工具

| 领域 | 首次 standalone 部署 | 用途 |
| --- | --- | --- |
| Linux/WSL x86_64 | 必需 | 当前自动化的支持平台 |
| NVIDIA 驱动与容器运行时 | 必需 | llama.cpp CUDA 运行 |
| Docker Engine + Compose v2 | 必需 | 容器生命周期与固定运行配置 |
| uv + Python 3.11+ | 必需 | Python 控制面；用户服务使用 `.python-version` 的精确版本 |
| `curl` + util-linux `flock` | 必需 | 本地健康探测和运行变更串行化 |
| Linux Node.js 24 | 可选 | `.nvmrc` 固定；ModelPort `standard/full` 与 Dashboard 语法检查 |
| Rust、Go、Java、Zig | 不需要 | 不应为 standalone 启动而安装 |

工具名出现在 `PATH` 中不等于可用。在 WSL 中要优先使用 Linux 原生可执行文件，
并用下列非变更命令确认后端真的可用：

```bash
python3 --version
uv --version
docker version
docker compose version
docker info --format '{{json .Runtimes}}'
nvidia-smi
curl --version
flock --version
```

Docker runtime 列表必须包含 NVIDIA 支持。仅有 `nvidia-smi` 成功不能证明容器能使用 GPU。

## Python 由 uv 管理

仓库用 `.python-version` 固定已验证的 Python，不要替换系统 `/usr/bin/python3`，也不需要
全局 `pip` 环境。

```bash
PYTHON_VERSION="$(tr -d '[:space:]' < .python-version)"
uv python install "$PYTHON_VERSION"
uv python find "$PYTHON_VERSION"
```

`scripts/install-user-services.py` 会通过 uv 解析这个版本，并把绝对 Python 可执行文件写入
渲染后的 systemd user unit。因此 shell 中的 `python3` 可以仅满足 3.11+，但长期运行
基线应先安装 `.python-version` 指定的 uv-managed Python。

uv 的常见用户级位置是：

- 配置：`~/.config/uv/uv.toml`（可以不存在）；
- Python：`~/.local/share/uv/python/`；
- 用户命令：`~/.local/bin/`；
- 缓存：`~/.cache/uv/`。

主机可以通过 `UV_PYTHON_INSTALL_DIR`、`UV_PYTHON_BIN_DIR` 和 `UV_CACHE_DIR` 改变这些根目录。
本项目只依赖 uv 解析结果，不把用户绝对路径写入 Git。

## 仓库配置分层

| 类型 | 路径 | 是否进 Git | 作用 |
| --- | --- | --- | --- |
| 模型 Catalog | `catalog/models.json` | 是 | 模型、revision、许可证、大小、SHA256 和容量门禁 |
| Node 版本 | `.nvmrc` | 是 | ModelPort 联合验收和 CI 的 Linux Node major |
| 运行 Profile 权威源 | `config/runtime-profiles.json` | 是 | latency/throughput 的类型化配置 |
| Profile schema | `config/schemas/runtime-profiles.schema.json` | 是 | 字段、类型和边界校验 |
| 派生 Profile | `profiles/latency.env`、`profiles/throughput.env` | 是 | `stack config` 可重建的 Compose 输入 |
| 容器模板 | `compose.yaml` | 是 | 镜像 digest、loopback 端口、挂载和安全项 |
| 当前模型选择 | `profiles/deployment.local.env` | 否 | `select/deploy` 生成的本机私有文件，必须 `0600` |
| Operations 凭据 | `profiles/operations.secrets.env` | 否 | 短生命周期 Collector 使用，不得交给 Dashboard |
| 备份配置 | `profiles/backup.local.env` | 否 | ModelPort checkout 和备份目录，可能含敏感信息 |
| systemd 加密凭据 | `profiles/credentials/*.cred` | 否 | 可选的 `systemd-creds` 后端 |
| 运行数据 | `models/`、`cache/`、`logs/`、`backups/` | 否 | 权重、事务、证据、报告与备份 |

有效 Compose 由“本机模型选择 + 固定运行 Profile + `compose.yaml`”渲染。调整 latency
或 throughput 时应修改 `config/runtime-profiles.json`，再用下列命令生成/检查派生文件：

```bash
./stack config check --json
# 只输出确定性渲染结果，不写文件
./stack config render --json
# 审阅渲染结果后才允许写入派生文件
./stack config render --write --yes
```

不要直接编辑 `deployment.local.env`来绕过 Catalog，也不要在 shell 中用同名环境变量
暗中覆盖 Compose。运行脚本会清理容器变量并根据上述文件重新渲染。

## systemd 与 WSL

用户服务渲染到 `~/.config/systemd/user/`。standalone 长期运行只需 runtime supervisor：

```bash
./scripts/install-user-services.py --check
./scripts/install-user-services.py --runtime-only --enable
systemctl --user status qwen-model-runtime.service
```

WSL 中的 supervisor 不会启动 Windows 或 Docker Desktop。Docker 后端未就绪时，它会保持运行并
定期重试，而不是绕过容量和完整性门禁。若要在无交互登录时保留 user manager，还需主机
管理者显式启用 linger。

WSL Windows 可执行文件失效时，Docker credential helper 可能报 `exec format error`。先在 Windows
PowerShell 中执行 `wsl --shutdown`，重新打开发行版后检查：

```bash
test -e /proc/sys/fs/binfmt_misc/WSLInterop
cmd.exe /c ver
docker-credential-desktop.exe version
docker version
systemctl --user status qwen-model-runtime.service
```

不要为了规避 credential helper 错误而改写或打印 `~/.docker/config.json`。公开且固定 digest 的
临时拉取可以经审查后使用一次性空 `DOCKER_CONFIG`，但这不是永久修复。

## ModelPort 可选环境

standalone API 不要求 ModelPort 或 Node.js。只有运行 `standard/full` 或启用联合运维时，
才需要兼容 ModelPort checkout 和 `.nvmrc` 固定的 Linux Node.js 24：

```bash
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm install
nvm use
node --version
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh standard
```

使用 NVM 时，确保非交互 shell 也会加载 NVM；不要依赖 WSL `PATH` 中的 Windows Node。
验收脚本拒绝其他 Node major，CI 也直接读取 `.nvmrc`，因此本机与 CI 使用同一版本契约。
ModelPort 管理员密码或 token 轮换后，重新运行支持的凭据 provision 流程，不要手工复制
值到日志或文档。

## 安全盘点

```bash
./stack doctor --json
./stack credentials audit --json
./stack verify --scope config
git status --short
```

本机 `.env`、Docker/npm/Git/Codex 配置、Cargo 凭据、云凭据和代理 URL 都可能敏感。诊断时只报告
路径、存在性、权限和可用性，不应打印内容。

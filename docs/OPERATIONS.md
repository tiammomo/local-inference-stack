# 运维与恢复

## 日常命令

优先使用只读公共入口：

```bash
./stack plan --json
./stack status --json
./stack doctor --json
./stack verify --scope config
./scripts/runtime.sh assert-profile latency
./scripts/runtime.sh logs
```

下列命令会改变 runtime，只在明确批准的维护窗口使用：

```bash
./scripts/runtime.sh start latency
./scripts/runtime.sh restart
./scripts/runtime.sh stop
```

`restart` 和 `start` 会先验证活动模型。启动、停止、重启、Profile 切换和候选发布由同一
`flock` 与持久事务串行化；启动只有在健康探针通过后才返回成功。默认 `latency` 是单 Slot；只有两个短上下文
任务并发时才显式切换 `./stack profile throughput --yes`，完成后恢复 `latency`。

公共 Profile 切换使用 `./stack profile throughput --yes`。若进程或 WSL 在切换/候选发布中断，
先运行 `./stack reconcile --json` 查看精确恢复动作，经确认后执行 `./stack reconcile --yes`。

必需的 standalone 健康入口：

```bash
curl --noproxy '*' http://127.0.0.1:18080/health
curl --noproxy '*' http://127.0.0.1:18080/metrics
nvidia-smi
```

只有显式启用 ModelPort/operations 后才应存在的可选入口：

```bash
curl --noproxy '*' http://127.0.0.1:38082/livez
xdg-open http://127.0.0.1:33004
```

所有端口只允许 loopback。本项目的 Python 本地 HTTP 客户端忽略环境代理、拒绝重定向并拒绝
非 loopback URL；Dashboard 还会校验 `Host` 和 WebSocket Origin。

## 长期运行

基础开机恢复：

```bash
./scripts/install-user-services.py --check
./scripts/install-user-services.py --runtime-only --enable
```

runtime-only 模式只保留 `qwen-model-runtime.service` 和本地告警模板，并禁用已有运营 unit；
日志与备份数据不会删除。supervisor 不负责启动 WSL 或 Docker Desktop：Docker 后端不可用时
每 60 秒重试，持续 10 分钟后记录 `0600` 本地告警但继续等待。成功后每 5 分钟校验
`/health`、固定 `latency` Profile 和实际容器身份；unhealthy 时只执行一次受控恢复。
Docker 临时不可用可以继续等待；显存准入、哈希、权限或配置漂移失败会停止并要求人工处理。

若要求 WSL 启动后无需交互登录即可运行 user manager，需要由主机维护者显式启用 linger：

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

## WSL 重启后的恢复检查

Docker Desktop、WSL systemd user manager 和 GPU 集成不保证同时就绪。已启用的 supervisor
会等待 Docker，因此日志中短暂出现 `Docker backend unavailable` 不等于部署失败。
按以下顺序复核：

```bash
docker version
docker compose version
docker info --format '{{json .Runtimes}}'
nvidia-smi
systemctl --user status qwen-model-runtime.service
./stack doctor --json
./stack status --json
curl --noproxy '*' http://127.0.0.1:18080/health
```

判定规则：

- `runtimeHealthy=true` 且 `/health` 为 `ok`：恢复完成，不要再次 deploy；
- plan 因空闲 VRAM 低而 `readyToDeploy=false`，同时 runtime 健康：现有模型占用 GPU，
  只是禁止新部署；
- `reconciliationRequired=false` 但 status 保留终态 `failed` 事务：这是审计历史，
  不是待恢复事务；
- supervisor 持续等待且 `docker version` 失败：先恢复 Docker Desktop/WSL 集成，
  不手工绕过容器准入。

若 Windows 命令或 `docker-credential-desktop.exe` 报 `exec format error`，先在 Windows
PowerShell 中执行 `wsl --shutdown`，重新打开发行版后验证：

```bash
test -e /proc/sys/fs/binfmt_misc/WSLInterop
cmd.exe /c ver
docker-credential-desktop.exe version
```

这个流程不需要读取、打印或改写 `~/.docker/config.json`。恢复后运行 quick 会生成
与当前驱动、容器身份和配置绑定的新凭证：

```bash
./scripts/acceptance-suite.sh quick
./stack plan --json
```

启用 ModelPort 运营、Dashboard、日报、备份与恢复演练：

```bash
cp profiles/backup.local.env.example profiles/backup.local.env
# 设置 MODELPORT_PROJECT_DIR
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
./scripts/install-user-services.py --operations --enable
systemctl --user --failed
```

ModelPort 管理员密码或 token 旋转后，旧的 `operations.secrets.env` 会出现 401。不要
手工对比或输出凭据；重新运行 `provision-operations-secrets.py --source ...`，再运行
`./stack credentials audit --json` 和 operations 验收。

运维凭据默认仍可从 `0600` 兼容文件读取。主机具有 `systemd-creds` 时，可以显式创建加密凭据；
命令不会删除原文件，确认服务重新安装并正常运行后再由维护者自行退役明文兼容源：

```bash
./stack credentials audit --json
./stack credentials migrate-systemd operations --yes
./stack credentials migrate-systemd backup --yes
./scripts/install-user-services.py --operations --enable
```

聚合报告不保存 Prompt、回复、工具数据、请求 ID 或凭据：

```bash
./scripts/operations-report.sh --hours 24
./scripts/operations-report.sh --hours 24 --fail-on-alert
```

systemd Collector 每五分钟短暂读取凭据并原子更新 `latest-{1,6,24,168}.json`；Dashboard
进程不再加载 ModelPort 管理员用户名、密码或备份凭据，只读取 `aggregate-only` 快照。ModelPort
尚未提供专用 operations scope 时，Collector 的上游权限仍是待双仓库完成的限制，不能由本仓库伪造。

## 备份与稳定性

```bash
./scripts/modelport-backup.sh create
./scripts/modelport-backup.sh verify
./scripts/modelport-backup.sh drill
./scripts/soak-check.py --minimum-hours 72
./scripts/soak-check.py --minimum-hours 168 --json
```

备份含数据库、配置和明文凭据：目录必须 `0700`、归档必须 `0600`，异机副本必须
加密。`drill` 在隔离 PostgreSQL 容器中恢复，不写生产库。72 小时用于灰度，168 小时
用于单机稳定基线；容器 recreate 会重新计时。单机备份不等于高可用。

## 缓存

Prompt RAM Cache 自动工作；稳定 system prompt、工具定义和规则放在前部，动态内容
放在尾部可提高命中。`slot-cache.sh` 保存的 KV 可能包含完整 Prompt，只能用于合成
前缀实验，保持 `0600`，不得提交或复制到公共存储。

存储盘点和清理默认只读；GC 仅把超过保留期的 `.part`/`.tmp` 列为候选，不会自动删除 GGUF、
当前部署、事务或回滚锚点：

```bash
./stack storage report --json
./stack storage gc --older-than-days 14 --json
# 审查精确路径后才允许：
./stack storage gc --older-than-days 14 --yes
```

## 故障处理

| 现象 | 处理 |
| --- | --- |
| CUDA OOM / 空闲显存不足 | 停止其他 GPU 负载，重新运行 `plan --json`；需要改容量时新建并验收 Profile，不临时绕过门禁 |
| Runtime 不健康 | 运行 `status`、`logs`、`verify --cached`；确认后再用受控 `restart` |
| ModelPort 找不到 `qwen-runtime` | 检查 `modelport_default` 网络、容器健康和 DNS alias |
| Operations Collector 登录返回 401 | ModelPort 凭据已旋转；用支持的 provision 流程重建本地私有凭据 |
| Docker credential helper `exec format error` | 检查 `WSLInterop`；优先完整 `wsl --shutdown` 后复核，不改写敏感 Docker 配置 |
| 只有 reasoning、没有正文 | 用精确 Token 计数检查输入，增加合理 `max_tokens` 或降低思考预算 |
| 磁盘/备份/systemd 告警 | 运行带 `--fail-on-alert` 的报告，验证最新备份并检查 user journal |

恢复顺序：先等待已启用的 runtime supervisor；若它未安装，经批准后用
`runtime.sh start latency` 恢复并确认 `18080/health`。然后恢复 ModelPort，最后运行
`acceptance-suite.sh standard`。不要用裸 Compose 命令绕过完整性和 Profile 检查。

更完整的概念、学习实验和排障路径见[学习与实践指南](LEARNING_GUIDE.md)。

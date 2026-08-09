# 运维与恢复

## 日常命令

优先使用只读公共入口：

```bash
./stack plan --json
./stack status --json
./stack status --scope integrated --json
./stack doctor --json
./stack verify --scope config
./stack verify --scope standalone
./scripts/runtime.sh assert-profile latency
./scripts/runtime.sh logs
```

下列兼容适配命令会改变 runtime，只在明确批准的维护窗口和现有事务保护下使用；不要用
`docker compose up/restart` 代替它们：

```bash
./scripts/runtime.sh start latency
./scripts/runtime.sh restart
./scripts/runtime.sh stop
```

`restart` 和 `start` 会先验证活动模型。启动、停止、重启、Profile 切换和候选发布由同一
`flock` 与持久事务串行化；启动只有在健康探针通过后才返回成功。默认 `latency` 是单 Slot；只有两个短上下文
任务并发时才显式切换 `./stack profile throughput --yes`，完成后恢复 `latency`。

公共 Profile 切换使用 `./stack profile throughput --yes`。当前 Catalog 冻结新部署期间，已有
选择仍可在主机准入通过后恢复，但不能借恢复入口下载、选择或部署新模型。若进程或 WSL 在切换/候选发布中断，
先运行 `./stack reconcile --json` 查看精确恢复动作，经确认后执行 `./stack reconcile --yes`。

`existing-selection` 恢复与新部署准入回答的是不同问题。已有 runtime 或其缓存会占用显存和
内存，因此恢复时探测到的当前 free VRAM/RAM 只作为诊断信息，不因低于新部署余量就拒绝恢复；
这不改变新部署的 `readyToDeploy=false` 硬停止，也不允许替换 Catalog、制品或 Profile。
恢复启动后，健康端点、实际容器身份和 canonical Profile 必须全部通过，任何一项失败都是硬门槛，
supervisor 不得把“这是已有选择”当成成功。

失败事务进入 `recovery_required` 并阻止后续变更。旧 schema v1 `failed` 记录不再被猜测为已恢复：
只读 reconcile 会先分类当前 runtime；只有显式批准后，才能在不触碰健康 canonical runtime 的
前提下标记 `superseded-verified`，或在恢复并验证原身份后标记 `failed-restored`。
处于 `planned/deploying/accepting` 的 v2 事务可能仍有发起进程存活，即使传入 `--yes` 也只会拒绝
并等待；只有明确的 `recovery_required` 才进入 v2 恢复路径。每次状态转换都必须匹配原事务 ID。
事务当前指针仍位于 `cache/control-plane/transaction.json`；每个经过验证的终态会在下一事务覆盖
指针前，以 `0600` 单文件归档到私有 `cache/control-plane/transactions/`。归档冲突或不安全路径
会 fail closed。早于该策略且已被覆盖的 legacy 迁移只能写成明确标注证据限制的本机 migration
ledger，不能补造终结时间或伪造原始事务文档。

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
Compose 将 runtime 的 `restart` 固定为 `no`，不接受 Profile 或环境变量覆盖；因此 Docker
Engine 只执行容器动作，不承担自动恢复。systemd user supervisor 是长期运行路径中唯一的
重启、等待和恢复 owner，standalone 模式不要求 ModelPort 服务已经运行。
Docker 临时不可用可以继续等待；显存准入、哈希、权限或配置漂移失败会停止并要求人工处理。
活动 transaction、维护 lease 或 runtime lock 冲突只会使 supervisor 等待；它不得在 deploy、
Profile 切换或候选发布期间擅自启动新选择，也不得把正常维护锁竞争当成永久故障。

本轮 Tier-1 主机的 owner-migration 维护切片已经完成：历史 schema v1 事务已显式解析为安全终态，
本机私有 selection 已按当前结构和权限规范化，实际容器 restart policy 已受控迁移为 `no`，
systemd user supervisor 已成为唯一自动恢复 owner。随后完成了一次“停止 runtime → 由 systemd
supervisor 恢复 → 健康与 canonical 身份复核”的演练，并通过维护后 quick recheck 的单测、制品、
直接生成和推理最小路径。该结果只证明当前运行身份和 supervisor 切换路径，不等同于完整升级、
回滚或主机 qualification，也不能替代一次真实的 WSL 关闭/重启验证。

这只是 Phase B 的 owner-migration slice。类型化 `stack upgrade/rollback`、quick 与完整
qualification 的统一事务覆盖、可持久恢复的 rollback spec，以及真实 WSL reboot 后的恢复证据
仍未完成。在这些门槛关闭前禁止开始 Phase C，尤其不得据此删除旧 reader、candidate 或恢复路径。

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

- `runtimeHealthy=true` 且 `/health` 为 `ok`：服务数据面可用，不要再次 deploy；若同时
  `controlPlaneReady=false`，命令会以 attention/恢复退出码返回，表示仍不能安全执行下一次变更；
- plan 因空闲 VRAM 低而 `readyToDeploy=false`，同时 runtime 健康：现有模型占用 GPU，
  只是禁止新部署；existing-selection 恢复把当前 free VRAM/RAM 视为 advisory，但启动后的健康、
  Profile 与实际身份仍是硬门槛；
- schema v2 的 `failed-restored`/`superseded-verified` 是已验证终态；旧 schema v1 `failed`
  必须由只读 reconcile 分类，不能仅凭时间或当前健康状态自动关闭；
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
freshness 检查还要求归档为当前用户拥有的单硬链接普通文件；时间戳最多允许 300 秒时钟偏差，
更远的“未来备份”会 fail closed，不能用负 age 冒充新鲜备份。
检查入库的 hardened user unit 只允许写 `PROJECT_ROOT/backups` 和告警目录；若配置外部
`MODELPORT_BACKUP_DIR`，必须同时提供经过评审的 systemd drop-in，将精确目标加入
`ReadWritePaths=`。否则 `ProtectSystem=strict` 会按设计拒绝写入，不能通过放宽整个文件系统绕过。

## 缓存

Prompt RAM Cache 自动工作；稳定 system prompt、工具定义和规则放在前部，动态内容
放在尾部可提高命中。本基线不提供手工 KV snapshot 入口；KV 状态可能编码完整 Prompt，
不应持久化、提交或复制到公共存储。

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

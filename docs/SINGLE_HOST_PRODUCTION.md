# 单机生产运行

## 定位

本方案面向一台可信 Linux/WSL 主机、一张 NVIDIA GPU、一个 llama.cpp 实例和一个
ModelPort PostgreSQL Compose 实例。它优化的是可恢复、可观测和变更可控，不提供
多节点高可用。GPU、主机、Docker 或 PostgreSQL 停止时会有服务中断。

默认生产边界：

- API、ModelPort 和运行台只发布到 loopback；
- Qwen 使用单 Slot 128K `latency` profile；
- 容器使用 `unless-stopped`，用户登录后由 systemd 幂等协调运行时；
- Docker `json-file` 日志有界轮转；
- 每日创建完整 ModelPort 备份，每周做隔离 PostgreSQL 恢复演练；
- 每日报告和实时运行台检查调用、GPU、磁盘、备份及 systemd 失败标记。

## 首次启用

先准备两个被 Git 忽略的本机配置：

```bash
cp profiles/backup.local.env.example profiles/backup.local.env
# 编辑 MODELPORT_PROJECT_DIR，使其指向实际 ModelPort checkout

scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
scripts/install-user-services.py --operations --enable
```

安装器会启用：

| 单元 | 周期/职责 |
| --- | --- |
| `qwen-model-runtime.service` | 登录/启动后幂等恢复 Qwen |
| `qwen-model-operations-dashboard.service` | WebSocket 实时运行台 |
| `qwen-model-operations-report.timer` | 每日 02:15 后生成脱敏报告 |
| `qwen-model-backup.timer` | 每日 03:15 后创建完整备份 |
| `qwen-model-restore-drill.timer` | 每周日 04:15 后隔离恢复最新备份 |

所有 timer 都使用 `Persistent=true`；关机错过后会在下次用户 systemd 启动时补跑。

## 备份、保留与恢复证明

手工执行：

```bash
scripts/modelport-backup.sh create
scripts/modelport-backup.sh verify
scripts/modelport-backup.sh drill
scripts/modelport-backup.sh latest
```

备份包包含 PostgreSQL custom dump、ModelPort `.env`、`config.toml`、SHA256 和源码/
镜像 provenance。目录权限为 `0700`，归档为 `0600`，默认保留 14 天。由于包含明文
凭证，它不能提交 Git，也不能复制到未加密的公共存储。

本机备份主要解决误操作、升级失败和数据库损坏；不能解决整块磁盘或整台机器损坏。
要获得真正的灾难恢复能力，应把 `backups/modelport/` 同步到加密、受控的异机介质，
并确保只有当前用户和恢复人员能解密。默认每日备份对应约 24 小时 RPO；RTO 必须以
实际整机恢复演练记录为准，当前不承诺固定值。

`drill` 使用没有宿主端口的新临时 PostgreSQL 容器，恢复后检查 `auth`/`control`
命名空间并删除容器。它不停止 ModelPort、不连接业务进程、不写生产数据库。真正的
灾难恢复按 ModelPort `docs/DOCKER.md` 执行，必须先停止 writer，保留故障现场和当前
数据库副本，再加载归档中的配置和 dump。

## 磁盘、日志与告警

Qwen 默认保留 5 个 20 MiB Docker 日志，ModelPort 的三个服务各保留 5 个 10 MiB
日志。可分别使用 `QWEN_LOG_MAX_SIZE`/`QWEN_LOG_MAX_FILES` 和
`MODELPORT_LOG_MAX_SIZE`/`MODELPORT_LOG_MAX_FILES` 调整，但修改后需要 recreate
对应容器。

运营报告默认在以下任一条件触发告警：

- 磁盘可用空间低于 10% 或 20 GiB；
- 没有 ModelPort 备份、最近备份超过 36 小时、或备份权限过宽；
- Qwen 不健康、ModelPort 未就绪、调用/Tool Use/延迟超过既有阈值；
- 备份、恢复演练、日报、Dashboard 或启动协调单元留下失败标记。

失败事件至少写入 `logs/alerts/` 和 user journal。若需要机器外主动通知：

```bash
cp profiles/alerting.local.env.example profiles/alerting.local.env
# 设置 OPERATIONS_ALERT_WEBHOOK_URL=https://...
scripts/install-user-services.py --operations --enable
```

Webhook 只发送时间、主机名和失败 unit，不发送 Prompt、回复、凭证、工具数据或原始
错误。只允许 HTTPS，或用于本机接收器的 loopback HTTP。

## 72 小时与 7 天稳定性门禁

代码验收通过不能替代连续运行证据。部署后执行：

```bash
scripts/soak-check.py --minimum-hours 72
scripts/soak-check.py --minimum-hours 168 --json
```

门禁要求 Qwen、ModelPort 和 PostgreSQL 连续运行达到目标时长、健康且零容器重启，
运营日报覆盖完整窗口且无告警，最近备份不超过 36 小时，备份权限正确，并且当前
部署清单全部通过。升级/recreate 会重新开始连续 uptime 计时，这是刻意的生产约束。

72 小时通过后可从灰度升级为“单机生产”；7 天通过后可标记为“单机稳定基线”。这仍
不等于高可用 SLA：需要零停机升级、故障自动切换或公网多租户时，应另建多节点方案。

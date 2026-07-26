# 运维与恢复

## 日常命令

```bash
./scripts/runtime.sh status
./scripts/runtime.sh logs
./scripts/runtime.sh start latency
./scripts/runtime.sh restart
./scripts/runtime.sh stop
./scripts/model-manager.py verify --cached
```

`restart` 和 `start` 会先验证活动模型。默认 `latency` 是单 Slot；只有两个短上下文
任务并发时才显式切换 `runtime.sh profile throughput`，完成后恢复 `latency`。

健康入口：

```bash
curl --noproxy '*' http://127.0.0.1:18080/health
curl --noproxy '*' http://127.0.0.1:18080/metrics
curl --noproxy '*' http://127.0.0.1:38082/livez
xdg-open http://127.0.0.1:33004
nvidia-smi
```

所有端口只允许 loopback。

## 长期运行

基础开机恢复：

```bash
./scripts/install-user-services.py --enable
```

启用 ModelPort 运营、Dashboard、日报、备份与恢复演练：

```bash
cp profiles/backup.local.env.example profiles/backup.local.env
# 设置 MODELPORT_PROJECT_DIR
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env
./scripts/install-user-services.py --operations --enable
systemctl --user --failed
```

聚合报告不保存 Prompt、回复、工具数据、请求 ID 或凭据：

```bash
./scripts/operations-report.sh --hours 24
./scripts/operations-report.sh --hours 24 --fail-on-alert
```

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

## 故障处理

| 现象 | 处理 |
| --- | --- |
| CUDA OOM / 空闲显存不足 | 停止其他 GPU 负载，重新运行 `plan --json`；需要改容量时新建并验收 Profile，不临时绕过门禁 |
| Runtime 不健康 | 运行 `status`、`logs`、`verify --cached`；确认后再用受控 `restart` |
| ModelPort 找不到 `qwen-runtime` | 检查 `modelport_default` 网络、容器健康和 DNS alias |
| 只有 reasoning、没有正文 | 用精确 Token 计数检查输入，增加合理 `max_tokens` 或降低思考预算 |
| 磁盘/备份/systemd 告警 | 运行带 `--fail-on-alert` 的报告，验证最新备份并检查 user journal |

恢复顺序：先用 `runtime.sh start latency` 恢复并确认 `18080/health`，再恢复 ModelPort，
最后运行 `acceptance-suite.sh standard`。不要用裸 Compose 命令绕过完整性和 Profile
检查。

# 运维手册

## 日常命令

```bash
cd /home/tiammomo/projects/infra/local-inference-stack

scripts/runtime.sh start
scripts/runtime.sh status
scripts/runtime.sh logs
scripts/runtime.sh restart
scripts/runtime.sh stop
```

默认 `latency` profile 提供单个完整 128K Slot。两个 Agent 同时工作且每路上下文
不超过 64K 时可临时切换吞吐 profile；完成后恢复默认：

```bash
scripts/runtime.sh profile throughput
scripts/concurrency-benchmark.py
scripts/runtime.sh profile latency
```

`throughput` 实测双请求聚合吞吐提高约 67.9%，但单请求速度下降约 18%，不适合
单 Agent 或长上下文任务。

ModelPort：

```bash
cd /home/tiammomo/projects/dev/ModelPort
docker compose ps
docker compose logs --tail=200 -f modelport
```

本地模型运行台：

```bash
xdg-open http://127.0.0.1:33004
systemctl --user status qwen-model-operations-dashboard.service
```

## 健康和监控

```bash
curl --noproxy '*' http://127.0.0.1:18080/health
curl --noproxy '*' http://127.0.0.1:18080/metrics
curl --noproxy '*' http://127.0.0.1:38082/livez
nvidia-smi
```

重点关注显存峰值、Prompt tokens/s、Generation tokens/s、首 Token 延迟、
请求失败率、上下文超限和容器重启次数。

聚合最近 24 小时真实调用、Tool Use、错误类别、llama.cpp 和 GPU 状态：

```bash
scripts/operations-report.sh --hours 24
scripts/operations-report.sh --hours 24 --save
```

`--save` 写入被忽略的 `logs/operations/`，权限为 `0600`。报告只含聚合值，不含
Prompt、回复、工具名/参数/结果、原始错误、请求 ID、用户、Key ID 或客户端 IP。
阈值、周期任务和问题到回归测试的映射见 [长期维护](MAINTENANCE.md)。
每日报告已由 user systemd 的 `qwen-model-operations-report.timer` 执行。

## 启动顺序

Docker Desktop 启动后，`restart: unless-stopped` 会恢复两个 Compose 项目。
若 DNS 或 provider 检查失败，按以下顺序恢复：

1. `docker compose up -d` 启动 llama.cpp。
2. 确认 `127.0.0.1:18080/health`。
3. 重建或重启 ModelPort。
4. 运行 `scripts/modelport-smoke.sh`。

## 常见故障

### CUDA OOM

先确认没有其他进程占用 GPU。仍然不足时，按顺序调整：

1. 将 `batch-size` 改为 1024、`ubatch-size` 改为 256。
2. 停止其他 GPU 工作负载，或降低上下文后建立独立 profile 并重新验收。
3. 最后才考虑部分 CPU offload；它会明显降低速度。

不要将 KV Cache 改为 q4：本机 A/B 中只要 K 或 V 使用 q4，92K 预填充便从约
3,100 tok/s 退化到约 200 tok/s 量级。128K 是本部署的功能目标。

### ModelPort 找不到 qwen-runtime

```bash
docker network inspect modelport_default
docker inspect qwen35-9b-q5km --format '{{json .NetworkSettings.Networks}}'
docker exec modelport-modelport-1 getent hosts qwen-runtime
```

### ModelPort 容器退出 127

WSL/Docker Desktop 重启后，单文件 bind mount 可能指向失效的内部路径。删除并
重新创建容器，不删除 volume：

```bash
cd /home/tiammomo/projects/dev/ModelPort
docker compose rm -sf modelport dashboard
docker compose up -d modelport dashboard
```

### 输出只有 Reasoning、没有最终答案

检查客户端 `max_tokens` 是否过小。复杂任务建议至少 8192，长链路思考建议允许
最多 32768；渲染后输入、思考和正文总量仍不得超过 131072。生产环境建议将
输入控制在约 92K。

服务默认启用推理。若直连 llama.cpp 的任务更重视低延迟，可按请求关闭：

```json
{"chat_template_kwargs":{"enable_thinking":false}}
```

经 ModelPort 调用时使用 Anthropic `thinking` 字段，或选择 `qwen3.5-fast`、
`qwen3.5-code`、`qwen3.5-deep` 逻辑模型；网关会映射请求级开关和预算。

### 首轮很慢、后续同前缀请求明显变快

这是预期行为。服务为提示缓存预留 8GiB RAM；92K 冷请求实测约 41.83 秒，同一
前缀的热请求约 7.37 秒。Agent 应尽量保持系统提示和稳定说明位于消息前部，
把易变内容放在尾部，以提高最长公共前缀命中率。

## 升级和回滚

1. 不使用浮动镜像启动生产配置；先记录新 digest。
2. 新镜像用临时端口完成冒烟和 118K 上下文验收。
3. 一次只升级推理引擎或模型中的一个。
4. 通过后修改 `compose.yaml` digest 并重新创建容器。
5. 回滚只需恢复旧 digest；模型文件和 ModelPort 配置不变。

# 本地模型运行台

模型名称、量化、上下文、输入/输出预算、Prompt Cache 和运行容器来自当前
`deployment.local.env`；静态 `runtime-baseline.json` 只作为尚未选择 Catalog Profile
时的 5070 Ti 回退。切换模型并重启运行台后，前端 WebSocket `hello` 会携带新的
baseline，避免在其他硬件上继续展示 9B 固定值。

## 入口

```text
http://127.0.0.1:33004
```

运行台是独立的只读页面，用于检查当前 Qwen、GPU、ModelPort 和实际调用情况；
ModelPort 原管理界面仍位于 `http://127.0.0.1:33002`。33003 已被 RoutePilot 占用，
因此本项目固定使用 33004。

## 展示内容

- 当前模型别名、GGUF 量化、llama.cpp 构建、128K 上下文和运行档位；
- Q8_0 KV、思考模式、提示缓存、Flash Attention、GPU offload 和 Tool Use 策略；
- GPU 显存、利用率、温度和功耗，主机内存、Swap 和 Load；
- Prompt/Generation tok/s、Slot 状态、处理与排队数量；
- 1 小时、6 小时、24 小时和 7 天真实调用成功率、Token 与延迟分位；
- Tool 请求可用性、协议通过率、合法调用、续轮最终回答、模型/Provider 分布、请求
  终态和活动告警；
- Qwen、ModelPort 和 ModelPort Dashboard 容器健康与镜像标识；
- SQLite 分层聚合历史形成的趋势图。

“当前 Qwen 路由”只展示本部署别名、`qwen3.5-*` 逻辑模型和 `local_qwen`
Provider，不混入 ModelPort 中其他模型。运行趋势采用独立双轨：上轨为请求成功率，
下轨为 P95 生命周期延迟；最多绘制 60 个采样点并保留完整快照数量，避免长时间运行
后出现密集点阵和宽屏 SVG 拉伸。

页面加载后只建立一条同源 WebSocket，不再轮询 HTTP API。后端按数据性质分层推送：

| 数据层 | 周期 | 内容 |
| --- | ---: | --- |
| `live` | 2 秒 | GPU、主机资源、Qwen `/health`、Slot、排队、吞吐和累计 Token |
| `status` | 5 秒 | 所选窗口内的调用、延迟、Tool Use、路由、账本信号、告警和前端实时趋势点 |
| `history` | 30 秒 | 服务端内存趋势与磁盘聚合快照的完整同步 |

时间窗切换会发送 `subscribe`，右上角刷新会发送 `refresh`；两者都是浏览器到服务端
的 WebSocket 消息，不会退回 HTTP 轮询。连接断开后前端按 1--10 秒指数退避自动重连，
重连后重新订阅当前时间窗。后端跨浏览器连接共享 2 秒实时缓存和 5 秒窗口缓存，并
复用 ModelPort 管理会话，避免重复采集和重复密码哈希登录。

前端每次收到 `status` 都会立即追加一个 5 秒趋势点；服务端每 30 秒写入
`logs/operations/history.sqlite3`。原始点保留 24 小时、分钟聚合保留 30 天、小时
聚合保留 365 天，查询会按时间窗自动选取分辨率。数据库只含请求数、可用率、延迟、
流式首语义 TTFT、缓存命中、Tool 协议通过率和资源指标，不含任何调用正文或标识符。

## 当前指标口径与待升级项

- `Tool 请求成功率` 只表示传输/协议请求完成；`协议通过率`、模型合法调用和续轮最终
  回答分别展示。真正的任务正确率来自 40 Case 闭环 Harness，不能从单个请求日志推断。
- `firstByteLatencyMs` 只对流式首个非空正文或 Tool Call 事件采样；非流式为空。延迟
  分位面板把 TTFT 和完整生命周期 E2E 使用两套独立比例尺，避免量级差掩盖变化。
- ModelPort 为调用记录有界 `trafficClass`；本项目所有合成验收显式标记 `synthetic`，
  运行台默认排除，同时兼容排除旧 `local_tool_acceptance_*` Mock Provider。
- Tool Use 面板显示 strict Schema、合法调用、续轮完成，以及受控修复的恢复/尝试数；
  不把修复后的 HTTP 成功解释为业务工具执行成功。

历史 `unknown_legacy` 仍不能解释为失败。下一步继续增加跨请求多步完成率和
Reasoning/正文 Token；完整定义见
[`ENHANCEMENT_ROADMAP.md`](ENHANCEMENT_ROADMAP.md)。

## WebSocket 协议

端点为 `ws://127.0.0.1:33004/ws`，当前 `protocolVersion=1`：

| 方向 | 类型 | 作用 |
| --- | --- | --- |
| 服务端 → 浏览器 | `hello` | 部署基线、协议版本与实际推送周期 |
| 服务端 → 浏览器 | `live` | 2 秒瞬时运行快照 |
| 服务端 → 浏览器 | `status` | 5 秒窗口聚合报告 |
| 服务端 → 浏览器 | `history` | 趋势采样点 |
| 浏览器 → 服务端 | `subscribe` | 切换 `1/6/24/168` 小时时间窗 |
| 浏览器 → 服务端 | `refresh` | 强制刷新当前时间窗和瞬时状态 |

保留 `/api/status`、`/api/history` 和 `/api/health` 作为诊断兼容接口，但前端不调用
前两者。

## 隐私和安全边界

服务仅绑定 `127.0.0.1:33004`，不监听局域网。浏览器只能获得
`operations-report.py` 产生的聚合数据：不包含 Prompt、回复、工具名、工具参数、
工具结果、原始错误、请求 ID、用户名、Key ID 或客户端 IP。ModelPort 管理密码只
存在于服务端 systemd 环境中，不进入 HTML、JavaScript 或 API 响应。

运行台和每日快照固定使用 `provider=local_qwen`，因此顶部成功率、Token、延迟、
Tool Use、路由与趋势均属于当前 Qwen 部署；ModelPort 全局指标仍在其管理界面查看。

状态服务：

```bash
systemctl --user status qwen-model-operations-dashboard.service
journalctl --user -u qwen-model-operations-dashboard.service --since today
curl --noproxy '*' http://127.0.0.1:33004/api/health
```

HTTP API 只支持 GET/HEAD，并返回 CSP、禁止 iframe、禁止 MIME sniff 和 no-referrer
安全头。WebSocket 握手强制版本 13、同源 `Origin` 和有效 Key，客户端帧必须掩码，
单帧上限 64KiB；跨站页面无法订阅本机运行数据。systemd 服务启用只读文件系统、
只读 Home、NoNewPrivileges 和地址族限制。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `dashboard/index.html` | 语义化页面结构 |
| `dashboard/styles.css` | 满宽、去卡片化的响应式数据布局 |
| `dashboard/app.js` | WebSocket、断线恢复、交互订阅、趋势和数据渲染 |
| `dashboard/runtime-baseline.json` | 不含秘密的部署基线 |
| `scripts/operations-dashboard.py` | 本机静态服务和只读聚合 API |
| `scripts/operations-report.py` | Qwen/ModelPort/GPU/主机数据源 |
| `logs/operations/history.sqlite3` | 权限 `0600`、有界保留的聚合时序库 |
| `deploy/systemd/qwen-model-operations-dashboard.service` | 长期运行和安全约束 |

模型、KV、上下文或端口基线变更时，更新 `runtime-baseline.json`，同时更新
`DEPLOYMENT_RECORD.md`。动态运行值以 llama.cpp `/props`、`/slots`、`/metrics`、
ModelPort 请求日志和 `nvidia-smi` 为准。

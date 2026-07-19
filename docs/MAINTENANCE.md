# 长期运营与维护

## 目标和边界

本项目与 ModelPort 作为同一套长期运行的本地推理基础设施维护。闭环是：

```text
实际调用 -> 聚合观测 -> 问题分类 -> 单变量 A/B -> 专项回归 -> 灰度上线 -> 记录基线
```

`infra/local-inference-stack` 负责模型制品、llama.cpp、GPU/内存、上下文和性能基线；ModelPort
负责客户端协议、路由、Tool Use、配额、请求账本和协议级可观测。默认入口始终是
ModelPort，`18080` 仅用于诊断和对照测试。

维护不以收集业务内容为代价。ModelPort 请求日志只记录路由、状态、Token、延迟、
成本估算、终态，以及请求是否处于 Tool Use 工作流；不持久化 Prompt、回复、工具
名、工具参数、工具结果或 Provider 原始报文。聚合报告进一步排除请求 ID、用户、
Key ID、客户端 IP 和原始错误。

## 观测入口

日常巡检优先打开只读运行台：

```text
http://127.0.0.1:33004
```

页面汇总当前模型、GPU/主机资源、推理吞吐、实际调用、Tool Use、服务链路和告警；
需要导出证据或自动化判断时再使用下面的命令行报告。页面服务状态可通过
`systemctl --user status qwen-model-operations-dashboard.service` 检查。页头显示
`WebSocket 实时流` 时，2 秒瞬时采样和 5 秒聚合推送均已建立；断线会自动重连，
持续显示断开时再查看服务 journal。

实时趋势写入有界的聚合 SQLite：原始点 24 小时、分钟点 30 天、小时点 365 天。
客户端取消与服务失败分开统计，SLA 使用服务可用率。`firstByteLatencyMs` 只记录
流式首个非空正文或 Tool Call 事件，非流式为空；运行台用独立比例尺对照 TTFT 与
完整生命周期延迟，不能把二者混成同一个分位。

运行台和日报只读取 `profiles/operations.secrets.env` 中的三个必要采集凭证，不加载
ModelPort 的数据库密码或 Provider Token。ModelPort 管理凭证变化后执行：

```bash
scripts/provision-operations-secrets.py
systemctl --user restart qwen-model-operations-dashboard.service
```

生成文件权限固定为 `0600` 并被 Git 忽略。

```bash
cd "$PROJECT_ROOT"

# 输出到终端
scripts/operations-report.sh --hours 24

# 仅统计当前 Qwen 部署；运行台和每日报告采用此口径
scripts/operations-report.sh --hours 24 --provider local_qwen

# 保存 0600 权限的聚合快照
scripts/operations-report.sh --hours 24 --save

# 有告警时返回非零，适合 systemd timer/cron
scripts/operations-report.sh --hours 24 --fail-on-alert
```

本机已启用 user systemd timer，每天本地时间 02:15 后随机 0--10 分钟执行，并在
关机错过后补跑：

```bash
systemctl --user status qwen-model-operations-report.timer
systemctl --user list-timers qwen-model-operations-report.timer
journalctl --user -u qwen-model-operations-report.service --since today
```

定时任务只读服务状态并保存聚合报告；有告警时 service 返回非零以进入 journal，
timer 本身仍会继续下一周期。单位文件位于 `deploy/systemd/`。

报告同时读取：

- llama.cpp `/health`、`/slots`、`/metrics`；
- ModelPort `/readyz`、`/metrics`、`/admin/logs` 和企业账本概览；
- `nvidia-smi` 的显存、利用率、温度和功耗；
- 最近时间窗的成功率、延迟、Token、Tool Use 请求成功率、终态和脱敏错误类别。

`trafficClass=synthetic` 的验收调用默认不进入生产成功率和告警分母，同时兼容旧
`local_tool_acceptance_*` Mock Provider；报告会给出排除数量，需要审计测试流量时加
`--include-synthetic`。`diagnostic` 仍纳入窗口，避免人工排障被静默隐藏。

ModelPort 部署新字段前的旧请求按 `toolOutcome=unknown_legacy` 兼容读取，因此 Tool
Use 结果趋势应从本次部署时间开始计算；历史工作流可以进入请求分母，但不能误标为
“未请求工具”。

Tool 请求成功率不表示工具已执行或最终答案正确。巡检应同时观察协议通过率、
`tool_called`、`final_answer`、`answered_without_tool` 和 40 Case 闭环门禁；历史
`completed` 只作兼容数据，不能重新解释为合法模型调用。
本项目脚本通过 `local_qwen` 运行的真实上游验收均发送显式 synthetic traffic class；
旧版本没有标签的历史请求仍需结合部署时间解释。

## 默认阈值

| 信号 | 默认告警条件 | 首要动作 |
| --- | --- | --- |
| 总失败率 | 最近窗口 `>=5%` | 按错误类别和模型拆分，检查是否集中在升级后 |
| Tool Use 请求失败率 | 有 Tool Use 样本且 `>=5%` | 检查严格响应错误，运行 Tool Use 专项验收 |
| P95 生命周期延迟 | `>=180s` | 区分长思考/长上下文与异常排队，核对 Slot 和 GPU |
| Timeout | 任意一条 | 对照 ModelPort 终态、llama.cpp 日志和客户端取消 |
| llama.cpp deferred | 当前大于 0 | 核对单 Slot 是否被并发请求占用，必要时临时吞吐档 |
| 账本 unreconciled/过期租约 | 超过已确认基线或当前存在过期租约 | 对照进程重启和 Provider 证据，不自动补记费用 |
| 报告截断 | 窗口记录超过抓取上限 | 提高 `--max-records`，并检查日志保留上限 |

阈值可通过 `--failure-rate-warn`、`--tool-failure-rate-warn` 和
`--p95-latency-ms-warn` 调整。深度思考或 92K 输入的慢请求不应直接判为退化；必须
按逻辑模型和上下文档位分组比较。

账本 `unreconciled` 是不可改写的历史证据。确认其不计费且完成归因后，在
`profiles/operations.env` 更新已确认总数；报告只对总数增加告警。本机当前基线为
1，来源是一次容器替换后的租约过期记录，`chargeable=false`。

## 维护节奏

### 每日或出现异常后

1. 生成最近 24 小时报告，检查服务健康、失败率、Timeout、Tool Use 和显存。
2. 若有告警，保留聚合报告并记录发生时间、逻辑模型、客户端协议和可复现的脱敏
   输入特征；不要复制生产 Prompt。
3. 先做只读诊断，不立即调低精度、上下文或严格协议校验。

### 每周

1. 比较连续快照的请求量、成功率、P50/P95/P99、输出 Token 和 Tool Use 请求成功率。
2. 查看 `logs/operations/` 是否按本机保留策略清理；它已被 `.gitignore` 排除。
3. 运行轻量回归：健康、普通生成、思考开关、Token 计数和 ModelPort 别名。
   统一命令为 `scripts/acceptance-suite.sh quick`。
4. 检查容器重启、磁盘、PostgreSQL 和模型文件校验状态。

### 每月或升级前

1. 备份 ModelPort 数据和 PostgreSQL，遵循 ModelPort `docs/OPERATIONS.md`。
2. 运行完整回归，包括 118K 召回、92K 解码、Tool Use 和 Provider matrix。
3. 记录镜像 digest、模型 SHA256、配置 diff、测试结果和已知限制。
4. 评估 5,000 条请求日志是否覆盖所需观察窗口；扩容前同时评估隐私和整文档
   持久化成本。

## 问题到回归的映射

| 实际调用现象 | 优先证据 | 必跑回归 |
| --- | --- | --- |
| 未声明工具、参数不是 Object、流式 JSON 不完整 | `tool_protocol`、请求终态、ModelPort 日志 | `scripts/tool-use-acceptance.sh --upstream --max-tokens 2048` |
| Tool Result 后重复调用或没有正文 | Tool Use 工作流记录、`is_error`、思考预算 | Tool Use 续接和 `is_error` 专项验收 |
| 只有思考、没有最终答案 | 输出 Token、逻辑模型、`max_tokens` | `scripts/modelport-reasoning-smoke.sh` |
| 长上下文遗忘或截断 | 输入 Token、Slot `n_ctx`、模板后总量 | `scripts/modelport-context-acceptance.sh` 和 118K 召回 |
| 首轮慢、后续快 | prompt tok/s、缓存复用、前缀稳定性 | 92K 冷/热 A/B，保持相同前缀 |
| 解码变慢或 GPU OOM | 显存、温度、功耗、decode tok/s | `decode-benchmark.py` 与生产基线对照 |
| 并发排队 | deferred、Slot 状态、P95 | latency/throughput profile 并发 A/B |
| ModelPort 502/504/499 | `terminalReason`、SSE 终态、上游容器日志 | ModelPort smoke、Provider matrix、客户端取消复现 |
| Token/费用异常 | billing mode、上游 usage、精确 Token 接口 | `modelport-token-count-smoke.sh` |

## 变更准入

1. 一次只改变一个主要变量：模型/量化、KV、上下文、Slot、采样、模板、推测解码
   或协议适配。
2. 在临时端口或独立容器上运行候选，不直接覆盖生产实例。
3. 性能变更必须同时通过质量、思考完成、Tool Use 和长上下文验收；只提升 tok/s
   不构成上线理由。
4. ModelPort 协议变更必须包含单元测试、mock 负例和至少一个真实本地上游用例。
5. 通过后再重建生产容器，随后生成一份运营报告作为新基线，并更新
   `DEPLOYMENT_RECORD.md`、`DECISIONS.md` 或 `OPTIMIZATION.md`。
6. 自动报告只能触发告警，不能自动改变 KV 精度、上下文、思考模式、Tool Use
   严格度或路由；这些变更必须经过 A/B 和回归。

候选版本通过 `scripts/release-candidate.sh` 串行运行在 `18081`。脚本用 trap 保证
成功、失败或中断后都清理候选，并仅在生产原本运行时恢复 `latency` 实例。质量门禁、
证据格式和回滚约束见 `QUALITY_AND_RELEASE.md`。

本机登录或重启后的幂等恢复由 `qwen-model-runtime.service` 执行；健康实例不重建，
缺失实例等待 ModelPort Docker network 后恢复。Docker 自身仍负责运行期容器重启，
systemd 单元不承担高频健康检查，避免两套控制器互相抢占。

## 数据保留和事件记录

- `logs/operations/*.json` 是聚合运行数据，默认文件权限 `0600`，不提交版本库。
- 原始 ModelPort 日志和数据库备份仍可能包含身份或网络元数据，按敏感运维数据
  保护；外发前使用 ModelPort 的 redacted diagnostic snapshot。
- 若聚合报告不足以定位问题，只保留人工构造、可复现、无秘密的最小样本，并记录
  预期行为、实际行为、模型/镜像版本和复现命令。
- 不改写企业账本的历史终态；账务或状态修正使用带证据引用的追加式调整。

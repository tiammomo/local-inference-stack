# 验收方案

## 必须通过

| 项目 | 通过标准 |
| --- | --- |
| 模型完整性 | 生产权重、MTP A/B 权重和 mmproj 三个文件 SHA256 均匹配 |
| GPU 后端 | 日志显示 CUDA、RTX 5070 Ti、全部模型层 GPU offload |
| 配置 | 128K、单 Slot、Flash Attention、Q8_0 K/V Cache 生效 |
| 健康 | llama.cpp `/health` 与 ModelPort `/livez` 返回 200 |
| 直连生成 | 中文确定性冒烟请求成功 |
| ModelPort | `/v1/messages` 经 `local_qwen` 返回成功 |
| Tool Use 协议 | 非流式、聚合后流式参数、continuation 通过；严格模式拒绝未声明工具和非法 JSON |
| 精确 Token 计数 | ModelPort 逻辑别名与 llama.cpp 直连对同一中文/Tool Schema 请求计数完全一致 |
| 上下文准入 | 超过 128K 总预算返回 400，错误包含精确用量且明确不做静默截断 |
| 思考长上下文 | ModelPort 约 92K 输入、最多 32K 输出时准确返回中部验收码 |
| 容量长上下文 | 直连关闭思考后，约 118K prompt 能准确返回中部验收码 |
| 稳定性 | 无 OOM、无容器异常重启、无持续显存增长 |
| 余量 | 峰值至少保留约 10% 显存 |
| 质量门禁 | 合成冒烟全部通过；正式升级运行三次重复集并保存聚合证据 |

## 执行命令

统一入口按变更风险分为三档：

```bash
scripts/acceptance-suite.sh quick
scripts/acceptance-suite.sh standard
scripts/acceptance-suite.sh full
```

`quick` 用于日常修改；`standard` 用于运行时或协议变更；`full` 用于模型、量化、
KV、上下文、Slot、镜像或重大版本升级。三档都会 fail-fast，且执行真实推理调用。

每次执行默认在 `logs/acceptance/` 保存权限为 `0600` 的文本日志和机器可读 JSON
证据，记录模式、结果、耗时、Git commit、运行镜像和配置摘要；只使用合成验收流量。
临时调试时可加 `--no-record`。运行态与版本化部署清单的一致性使用：

```bash
scripts/verify-deployment.py
```

需要单项复验时使用以下底层命令：

```bash
cd /home/tiammomo/projects/infra/local-inference-stack
scripts/verify-models.sh
scripts/smoke-test.sh
scripts/reasoning-smoke.sh
scripts/modelport-smoke.sh
scripts/modelport-reasoning-smoke.sh
scripts/modelport-token-count-smoke.sh
scripts/modelport-context-admission-smoke.sh
python3 scripts/quality-eval.py --smoke
python3 scripts/context-acceptance.py
scripts/modelport-context-acceptance.sh
python3 scripts/decode-benchmark.py
python3 scripts/concurrency-benchmark.py
scripts/runtime.sh status
```

ModelPort 长上下文验收默认构造约 92K 输入，并为思考和正文保留最多 32,768
tokens；直连 118K 用于验证容量，脚本显式关闭思考。两者都要求最终正文精确匹配，
不能只在 `reasoning_content` 中出现验收码。

## 流式请求

通过 ModelPort 发起 `stream=true` 请求，必须看到合法的 `message_start`、
content block 增量和结束事件。HTTP 200 但缺少终止事件不算通过。

## Tool Use

基线模型服务稳定后运行 ModelPort 的真实上游测试：

```bash
cd /home/tiammomo/projects/dev/ModelPort
scripts/provider-matrix.sh --model qwen3.5-code
scripts/tool-use-acceptance.sh --upstream --max-tokens 2048
```

Tool Use 失败不影响纯聊天服务验收，但在接入 Agent/Claude Code 前必须修复或
明确禁用对应能力。

本地验收使用 `streaming_arguments="best_effort"` 和
`response_validation="strict"`。流式事件中可以只出现一个完整
`input_json_delta`；验收仍应拼接全部 delta 后解析，且只有收到
`content_block_stop` 与 `stop_reason=tool_use` 才能执行工具。

## 2026-07-18 128K 实际结果

上述必须项全部通过，包括默认思考、ModelPort 流式和 Tool Use。直连关闭思考的
118K 容量复验使用 118,062 prompt tokens，42.61 秒返回验收码；ModelPort 默认
思考链路计入 92,063 input tokens，冷缓存 39.26 秒返回相同结果；此前同前缀热缓存
为 7.37 秒。
两项均无 OOM 或截断。完整数据见 [DEPLOYMENT_RECORD.md](DEPLOYMENT_RECORD.md)。

精确 Token 计数也已通过：包含中文 system、混合消息和 Tool Schema 的请求在
llama.cpp 直连与 ModelPort `qwen3.5-code` 别名下均为 282 tokens；关闭思考的实际
模板映射双方均为 15 tokens。

双 Slot 不是必须生产项，但其 profile 已通过两路并发 A/B：聚合吞吐
138.71 tok/s，相对单 Slot 82.60 tok/s 提升约 67.9%。验收后已恢复单 Slot 128K。

质量集只包含可公开的合成输入。`logs/quality/` 的证据仅记录 Case ID、通过状态、
Token 和延迟，不保存 Prompt 或回复；完整正式门禁使用 `--trials 3`，避免一次采样
偶然通过。

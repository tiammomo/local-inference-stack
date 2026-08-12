# 架构与边界

## 请求路径

```text
应用 / Agent
  └─ 可选 ModelPort（Anthropic Messages、认证、路由、账务、Tool 协议）
       └─ qwen-runtime:8080/v1
            └─ llama.cpp CUDA + 本地 GGUF

本机诊断 ──> 127.0.0.1:18080/v1（OpenAI-compatible）

凭据化 ModelPort 聚合读取 ──> 短生命周期 Collector
  └─ aggregate-only JSON 快照 ──> 零凭据 Dashboard
```

ModelPort 不是首次部署前置条件。应用负责真实工具执行、审批、沙箱、幂等和业务判断；
本仓库不执行工具。

## 职责

| 层 | 负责 |
| --- | --- |
| 本仓库（当前迁移态） | 模型选择、制品完整性、运行时、容量 Profile、验收、类型化 rollout、候选发布、聚合运维；ADR-0002 要求将聚合运维迁出 |
| ModelPort | 认证、逻辑模型、Anthropic/OpenAI 边界、Token 准入、Tool Use 适配 |
| 应用 / Agent | 上下文管理、工具选择与执行、权限控制、最终结果验证 |

权威数据源：

- `catalog/models.json`：模型、制品、哈希与容量；
- `config/runtime-profiles.json`：运行 Profile 的类型化权威来源；
- `profiles/*.env`：确定性派生文件，`./stack config check` 防止手改漂移；
- `compose.yaml`：通用运行模板，其默认值受类型化配置校验；
- `contracts/local-qwen-provider-v1.json`：跨仓库协议；
- `deployments/*/manifest.json`：历史验证身份、当前仓库配置和晋级状态；三者不能互相冒充。
- `cache/control-plane/transaction.json`：当前受锁变更及其 rollout intent/action journal；
- `cache/control-plane/rollback-specs/<sha256>.json` 与 `rollback.json`：私有 content-addressed
  immutable anchor 和单调 active pointer，不进入 Git。

运行参数分为两层：Catalog 模型层拥有 context、输出上限、cache、KV 类型、batch 和
ubatch；`latency`/`throughput`/`candidate` 模式层只拥有并发、端口和候选生命周期等模式参数。
两层键集合必须互斥，最终有效 Compose 对每个 Catalog 条目做确定性测试。

## 当前运行设计

| Profile | Slot | 每 Slot 上下文 | 用途 |
| --- | ---: | ---: | --- |
| `latency` | 1 | 128K | 默认，单任务和长上下文 |
| `throughput` | 2 | 64K | 显式切换，双任务吞吐 |

默认开启思考，建议渲染后输入不超过约 92K，最多输出 32,768 tokens。16GB 单卡不能
同时驻留生产和完整目标：兼容 `release` 仍采用停产后的串行候选并自动恢复生产；类型化
upgrade 则在同一持久事务中停止源、启动目标并在成功后发布源的 rollback pointer。两者都不允许
手工启动第二个 GPU runtime。

## 设计约束

- 当前自动新部署只支持 Tier-1 WSL2 x86_64、精确 RTX 5070 Ti 单 GPU 档案；native Linux
  和其他 NVIDIA 型号在取得真实 evidence 前保持只读。
- 服务只绑定 loopback；本项目不提供公网、多租户或高可用。
- runtime 不加入 Compose default bridge，只加入显式 ModelPort 网络；该容器侧 API 无独立认证，
  因而网络内所有成员仍属于可信边界，`--offline` 不能替代 egress firewall。
- llama.cpp 镜像、模型和 GGUF 均固定不可变身份。
- 推荐、下载、上下文准入和验收默认 fail-closed。
- `validated` 档位不等于新主机已验证；本机凭证必须仍然匹配且有效。
- 运行时使用 OpenAI-compatible；Anthropic Messages 由可选 ModelPort 提供。
- Docker restart policy 固定为 `no`，不能由 Profile 或环境变量改写；systemd user supervisor
  是唯一的启动等待、监控与自动恢复 owner。Compose 和兼容脚本只是受锁动作适配器。
- 生产保留 Q8_0 KV、单 Slot、文本模型和有界缓存；其他组合必须候选验收。
- ModelPort 契约变更必须协调两个仓库，不能在本仓库复制其实现。
- `cache/control-plane/transaction.json` 原子记录运行变更；`recovery_required` 和所有未知/legacy
  失败都会阻止下一次变更。只有恢复并验证事务预先固定的 recovery subject（普通变更为原
  runtime，rollback 为已验证锚点），或显式验证健康 runtime 已安全 supersede 旧失败后，事务才能
  进入终态。
- runtime flock 与 transaction identity 共同保护变更；supervisor 和兼容脚本不能绕过活动事务。
- 每次事务 transition 都对 transaction ID 做 compare-and-swap；旧进程的延迟失败不能污染新事务。
- upgrade/rollback 的 rollout intent 还固定 source/target Catalog spec、rollback spec、pointer 前驱和
  完整 action plan。每个 runtime mutation 只能消费下一 action 的精确 ordinal、kind、subject 与
  artifact identity；结果摘要写入 action journal 后才能前进。
- rollback-spec v1 的唯一 scope 是 `same-controller-same-catalog-anchor-v1`：同一 WSL2 x86_64
  主机、同一干净 controller revision/material、当前 Catalog 中仍可信的 rollback entry、
  `latency` Profile，以及本地 GGUF 和固定 image ID。rollback 不允许网络获取、image pull 或 Git
  checkout；身份漂移只会阻断，不会自动“修复”权威来源。
- active rollback pointer 只在 upgrade 目标 quick 成功且锚点再次验证后发布，在 rollback quick
  成功后以单调 tombstone 清除。pointer CAS 与 transaction recovery 共同关闭进程中断窗口。
- rollout 中的 `quick --no-record` 只是服务门禁，不产生 host acceptance 或 qualification 结论；
  Catalog 晋级仍要求独立可信 full evidence。
- 恢复记录只保存 allowlisted 结构化 Profile 值和完整运行身份，不保存稍后会被 shell `source` 的
  原始文本。健康原实例缺少身份哈希时禁止自动恢复。
- 本机 schema 各自声明可读版本；旧 Profile/transaction 与纯制品 bundle 有受限兼容，旧 attestation
  因信任语义不足而拒绝。任何现场处理都必须显式批准并保留回滚，不允许静默重写。

当前实现是 Phase B 的类型化基础，不是 Phase B 终态。还需要把 full qualification runner 的完整
记录和结论绑定到同一 rollout transaction，在合法 validated rollback/LTS 模型对上完成 Tier-1
真实 upgrade→rollback drill，并验证 WSL shutdown/reboot、Docker 延迟就绪和事务重入。当前
Catalog 只有 provisional 条目，因此这些真实 rollout 会按设计 fail closed；不能以手写锚点绕过。

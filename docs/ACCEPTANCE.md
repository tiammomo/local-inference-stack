# 验收与发布

## 验收分层

| 模式 | 覆盖 | 何时运行 |
| --- | --- | --- |
| `quick` | 单测、活动权重、运行态、直连生成与思考 | 首次部署、WSL/Docker 恢复、普通本仓变更 |
| `standard` | quick + ModelPort 契约、Token、Dashboard、Tool Use、质量冒烟 | 协议、推理或 Tool 变更 |
| `full` | standard + 完整哈希、长上下文、性能门禁、完整质量集 | 模型、量化、KV、上下文、镜像升级与 Catalog 晋级 |

```bash
./scripts/acceptance-suite.sh quick
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh full
```

所有模式 fail-fast。`standard/full` 只支持当前版本化的 9B Provider 契约，并要求显式
提供兼容 ModelPort checkout；它们还会调用 ModelPort 的 JavaScript 验收工具，因此
Linux Node.js 必须匹配 `.nvmrc` 的 24 major。使用 NVM 时先执行：

```bash
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm install
nvm use
node --version
```

### rollout 内部 quick 与 full 的边界

类型化 upgrade/rollback 在切换前后调用内部 `quick --no-record`。它绑定到当前 transaction 的
下一 action，只作为“这个精确 runtime 能否继续完成或回退”的服务门禁；`--no-record` 明确禁止
产生新的 schema v4 host evidence。因此一次成功的 upgrade/rollback quick：

- 不构成模型、量化、镜像、上下文或主机的 qualification；
- 不会把 `evidenceStatus` 改成 `validated-on-this-host`；
- 不能签署 reusable attestation、晋级 Catalog 或改写 deployment manifest 的历史验证结论。

upgrade 默认的 `--qualification quick` 保持上述语义。需要在 replacement 上产生新 qualification
时，使用公共入口的 v2 rollout；不要直接拼装内部环境变量或调用 runner：

```bash
MODELPORT_PROJECT_DIR=/absolute/path/to/ModelPort \
  ./stack upgrade --model CATALOG_ID --qualification full
MODELPORT_PROJECT_DIR=/absolute/path/to/ModelPort \
  ./stack upgrade --model CATALOG_ID --qualification full --yes
```

`full` 会在 target quick 之后加入独立 `target-full` action。它先绑定一个确定性 schema v2 run
manifest，再生成 schema v5 evidence；文件名固定为
`qualification-<transaction-uuid>-<action-ordinal>{.run.json,.json}`。两份文件共同绑定 rollout plan、
source/target Catalog spec、rollback spec、action、acceptance configuration、controller materials、
精确容器 ID/启动时间/image ID/config hash，以及 ModelPort 的 clean Git commit/tree、关键材料哈希
和匹配的 loopback live build。每个 step 与 finalize 都重新检查冻结输入，runner 子进程不继承
rollout mutation authority。

ModelPort source identity v2 还要求 `/livez` 的 `build.configSha256` 精确等于安全读取的受审
`config.toml`。compatibility 结果必须绑定它实际读取的 raw provider contract、config 和 governance
源码摘要；不能把两次不同路径读取的结论拼成一个输入。鉴权 `/v1/models` registry 只证明受审
logical aliases 当前由 `local_qwen` provider 暴露，不自称能证明背后的 physical target；物理目标
由事务绑定的 target runtime 和后续 gates 证明。local Tool 请求固定使用 `local_strict`，不得在
qualification 中静默回退到云 provider。

passed 文件本身仍不是权威结论。控制面会在同一 runtime boundary 内安全重读两份私有文件、
校验逐步结果与所有哈希，并把 receipt 以 CAS 写入当前 `target-full` action；只有随后完整结束的
upgrade transaction 才能反向解析该 schema v5 qualification。写出 evidence 后崩溃、事务失败或
恢复、文件替换、错误 transaction/action/spec，以及没有 completed receipt 的复制件全部失效。
receipt 完成前不会发布 rollback pointer。

当前运行中的 ModelPort 尚未提供所需的 `/livez build.configSha256`，因此本机
`--qualification full` 会在事务创建和 source 停机前 fail closed。必须先在独立、受评审的跨仓
升级中补齐该非秘密 live identity，再执行真实 drill；不能放宽 v2 reader 或手工补字段。

### standard/full 联合前置条件

| 前置项 | 通过条件 | 典型修复 |
| --- | --- | --- |
| ModelPort checkout | `MODELPORT_PROJECT_DIR` 指向含联合验收脚本的兼容 checkout | 先运行双仓库 compatibility check |
| Linux Node | `command -v node` 不是 `/mnt/...`，major 为 24 | 在仓库目录 `nvm install && nvm use` |
| ModelPort runtime | loopback `http://127.0.0.1:38082/livez` 健康 | 按 ModelPort runbook 启动兼容版本 |
| 调用凭据 | `profiles/operations.secrets.env` 含当前测试 token，权限保持私有 | 用 provision 脚本从受控来源刷新 |
| 聚合快照 | `logs/operations/latest-{1,6,24,168}.json` 来自当前凭据和 contract | 短生命周期运行 Collector |
| Dashboard | `127.0.0.1:33004` loopback 健康，24h status 满足契约 | 启动前台进程或 reviewed user unit |

需要临时执行联合验收、但不希望常驻启用 operations units 时，可在已获准的测试窗口运行：

```bash
./scripts/provision-operations-secrets.py --source /path/to/ModelPort/.env

set -a
source profiles/operations.secrets.env
set +a
python3 scripts/operations-collector.py

# 保持此前台进程运行，在另一个终端执行 standard/full
python3 scripts/operations-dashboard.py
```

然后在第二个已执行 `nvm use` 的终端运行 `standard/full`。验收会在 quick 和模型请求前检查
Node major、ModelPort `/livez` 以及 Dashboard 的 health/status 契约；缺失或快照不可读时立即退出，
不会先消耗 GPU 测试时间。`401` 通常表示本地 operations 凭据已落后于 ModelPort 当前状态，应重新
provision，不能把密码或 token 粘贴进命令、日志或文档。

## 本机凭证

验收日志和 JSON 写入 Git 忽略的 `logs/acceptance/`。独立验收继续写 schema v4；事务绑定 full
写 schema v5。两者都只有在通过当前读取策略时才可能产生 `validated-on-this-host`，其中 v5
还必须反向匹配 exact completed upgrade receipt。共同条件包括：

- 当前用户所有、普通文件、单硬链接、权限 `0600`；
- 未超过 30 天，时间和正文自校验正确；
- 机器指纹、GPU/驱动、模型、GGUF、镜像和关键依赖哈希全部匹配；
- 验收 Profile 为 `latency`，模式对应的测试依赖没有漂移；
- 本地选择 Profile、有效 Compose 哈希和实际容器配置哈希全部匹配；
- 实际容器仍在运行且健康，镜像 ID、用户、命令、安全项、日志、端口和关键挂载没有漂移。
- 当前模型文件的设备号、inode、大小、mtime、ctime 与私有 integrity stamp 完全匹配；任一变化
  都会失效并要求重新计算完整 SHA256，而只读 plan 不会偷偷重哈希大文件。
- 实际容器的 privileged、capability、网络/alias、全部挂载、GPU device request、tmpfs、PID、
  init、restart 和日志边界与 canonical Compose 完全匹配；完整信封还精确绑定镜像 ID、Entrypoint、
  WorkingDir、healthcheck、环境变量键集合与隐私哈希、PID/IPC/UTS/user/cgroup namespace、raw
  devices、bind mode/propagation、DNS/extra-host/sysctl，结果只输出漂移字段而不输出环境值。

复制到其他主机、配置漂移、过期或权限放宽都会使凭证失效。硬件档位匹配不能替代它。

## 验收后如何解释 plan

quick 通过后重新运行：

```bash
./stack plan --json
./stack status --json
```

当前主机与配置完全匹配时，plan 会返回：

- `evidenceStatus=validated-on-this-host`；
- `hostAcceptanceStatus=passed-current-configuration`；
- `hostAcceptanceEvidence` 指向当前安全的 schema v4 文件，或带 completed transaction receipt 的
  schema v5 文件。

这不保证 `readyToDeploy=true`。健康容器已占用 GPU 时，空闲显存会使
`readyToDeploy=false` 且 `actionPlan=null`，目的是阻止第二次部署。现有服务是否已恢复
应以 `status.facts.runtimeHealthy`、`/health` 和 canonical Profile 检查为准。

若 quick 失败，失败证据只用于诊断，不会升级本机状态。不要手工编辑 JSON、
放宽权限或复制旧证据来获得 `validated-on-this-host`。

## Catalog 晋级与性能证据

本机 acceptance 与可复用 Catalog attestation 是两层证据。新的 transaction-bound qualification
使用当前、完整的 schema v5 `full` evidence，并要求 exact completed receipt、clean Git revision、
当前制品/runtime/配置匹配、受信任的分离
签名、有效期、撤销状态和 supersede 链。自哈希、README 或 manifest 不能单独完成晋级。
schema v4 继续用于 standalone 本机证据兼容；它不能冒充缺失的 rollout binding 或 v5 receipt。
当前晋级策略仍把满足全部既有条件的 standalone schema v4 `full` 保留为临时 legacy bridge，
因此并非所有可复用资格都已经强制迁到 v5。待 typed candidate qualification 覆盖旧流程并完成
实机验证后，才能另行评审、迁移消费者并删除这条 bridge。
稳定 validation input 还绑定全部控制面 package、公共 launcher、类型化配置 schema 和性能策略；
任何安全/晋级逻辑或硬阈值变化都会使旧签名失去晋级资格。Catalog 运行时只接受由
`LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY` 与
`LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY_SHA256` 提供的外部固定信任根。
签名前会从保留的 `0600` evidence 重新构造 canonical subject，逐项绑定 model、validation input、
artifact、runtime 和完整 runner 记录；不能把 A 模型证据换成 B 模型声明。验签时公钥和 signature
必须是当前用户拥有、单硬链接、不可被 group/other 写入的普通小文件；哈希和外部验签只使用同一
份 `0600` 快照，避免路径在两步之间被替换。

性能策略来自 deployment manifest。长上下文正确性、无 OOM、峰值显存、稳定解码吞吐和聚合
吞吐属于硬门禁；TTFT、预填充和其他高噪声指标先记录分布并告警。没有至少三轮校准数据时，
策略保持 `pending-baseline`：可以收集 baseline，但生成的 full evidence 不具备晋级资格。

校准会发送合成模型请求，必须单独批准；它同时采集 decode、aggregate throughput 与峰值显存，
只写 `0600` 私有报告，不改生产 Profile，也不会产生 host acceptance：

```bash
./stack calibrate plan --json
./stack calibrate run --output logs/calibration/run-1.json --yes
```

至少重复三轮并评审分布后，才能在 manifest 中把策略改为 `enforced`。普通 `full` 在
`pending-baseline` 时会在任何模型请求前退出；`full --baseline-only` 强制 `--no-record`，即使所有
测试通过也不能签署或晋级。

当前可执行 Catalog 只保留 legacy/provisional 9B 档案，并暂时禁止新部署。未经实机验证的估算
模型不再进入部署 allowlist。

## 发布前检查

```bash
./scripts/release-check.sh
./scripts/model-manager.py audit-sources --json
./stack verify --scope all --json

python3 ./scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR" --release
```

`standalone` 会验证完整容器身份与当前制品，`integrated`/`all` 会先通过同一
standalone/transaction 基线，再精确检查 ModelPort、
PostgreSQL、Dashboard 的权限、全部端口/挂载/网络安全信封，以及 operations、备份和历史部署
身份。`release-check` 包含全部 JSON/Python/Shell/前端语法、单测、Markdown 链接、三个 Compose
Profile、部署 manifest 文件哈希、Git 禁入项和 Gitleaks。来源审计会联网
核对固定 revision/LFS 身份，但仍不能替代许可证和发布者信任审查。

当前历史 manifest 没有足够信息重建三个 ModelPort 容器的完整 executable identity，因而显式标为
`review-required`，不能从当前 dirty live container 反向填充成“已评审”。这是联合验收的预期阻断项，
不是通过放宽 verifier 消除的告警。
未来的 `reviewed-current` identity 必须绑定 Compose 与非秘密 `config.toml` 内容哈希；私有 `.env`
不把秘密摘要写入 Git，而是在安全读取后把源值与 live container environment 在内存逐键比较，
诊断只输出字段名。缺失 bindings/files 字段本身就是失败，不能用省略字段跳过验证。

质量或 Tool Use 定位时使用；这些命令要求已配置可用的 ModelPort：

```bash
python3 scripts/quality-eval.py --smoke
python3 scripts/quality-eval.py --trials 3
python3 scripts/tool-workflow-eval.py --smoke
python3 scripts/tool-workflow-eval.py
python3 scripts/tool-workflow-eval.py \
  --cases quality/tool-resilience-workflows.json
```

证据只保存 Case ID、数值和结果，不保存 Prompt、回复或工具数据。

## 串行候选与回滚

16GB 单卡不能并驻生产与完整候选。以下命令会停止生产并造成短暂中断，只能在用户
明确批准的维护窗口执行；脚本会在停产前检查 ModelPort checkout，保存原生产 Profile，
并且无论成功、失败或中断都会恢复原生产：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./stack release quick --yes
```

晋级要求：

1. 一次只改变一个主要变量，并固定镜像 digest、制品 SHA256 和 Profile。
2. 按风险通过 quick、standard/full、质量、上下文、显存和恢复检查。
3. 更新 Catalog/Compose/manifest 与必要文档，再运行发布前检查。
4. 重建生产后运行 `standard`；72 小时用于灰度，168 小时用于稳定基线。

失败时恢复上一 Git revision、镜像 digest、Catalog 制品和 Profile，再运行 quick。
旧 GGUF 删除前必须完成回滚演练；任何 recreate 都会重置连续运行计时。

上面的 `release` 是兼容性串行候选流程，不是类型化生产 upgrade。正式路径是先只读审阅
`./stack upgrade --model CATALOG_ID` 或 `./stack rollback`，再显式批准对应 `--yes` 命令；详细
scope 和恢复规则见[升级与回滚](UPGRADING.md)。rollback 只使用同主机、同 controller、当前
Catalog 仍认可的本地 immutable anchor，不联网、不 pull、不 checkout Git，并固定恢复
`latency`。当前 Catalog 只有 provisional 条目，不能形成合法 validated rollback/LTS 模型对，
所以真实 upgrade/rollback 按设计 fail closed。

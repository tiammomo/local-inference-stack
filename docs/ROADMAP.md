# 项目优化路线图

本路线图落实 [ADR-0001](decisions/0001-trusted-single-host-appliance.md) 和
[ADR-0002](decisions/0002-long-term-appliance-simplification.md)。发生冲突时，以更窄、更新的
ADR-0002 为准。它描述依赖顺序和完成定义，不是承诺日期；任何阶段都必须保持现有安全黄金
路径可用。

截至 2026-08-12，控制面完成了一轮 fail-closed 加固，但当前 Catalog 的唯一 9B 条目仍是
`provisional`：在线健康实例不等于新部署或 replacement 资格。package-native 权威边界、无消费者
wrapper 清理、Tier-1 owner migration 和类型化 upgrade/rollback 基础已经落地。维护后 quick
recheck 已通过，但它不是 full host qualification；Catalog 没有晋级，历史验证结论也未重写。

## ADR-0002 收口阶段

| 阶段 | 当前状态 | 完成定义 |
| --- | --- | --- |
| A 旁路重构 | 进行中 | Catalog、material、observation 和 lifecycle 逻辑进入 package；当前 runtime 不变 |
| B 维护窗口切换 | typed rollout 基础已完成，实机收口进行中 | systemd 是唯一自动恢复 owner；仍须把 full qualification 完整绑定到 rollout transaction，并完成真实 upgrade/rollback、reboot 与 host qualification |
| C 验证后删除 | 未开始 | LTS/回滚/重启验证通过后，删除旧 schema、candidate、operations、bundle 与 attestation 核心路径 |

### Phase B owner migration 与 typed rollout 基础

本轮已完成并在当前 Tier-1 主机验证：

- Compose 和实际容器均使用 `restart: no`，Docker 不再拥有自动恢复策略；
- systemd user supervisor 是唯一的等待、监控和自动恢复 owner；
- 历史 schema v1 事务已显式解析，本机私有 selection 已按当前结构和权限规范化；
- “停止 runtime → systemd 恢复 → post-start 健康、Profile 与实际身份检查”演练通过；
- 维护后 quick recheck 的单测、制品、直接生成和推理最小路径通过，但不产生晋级资格。
- `stack upgrade`/`stack rollback` 已具有精确 Catalog source/target、持久 rollout intent、固定 action
  plan、逐 action CAS 授权与结果 journal；所有状态改变仍要求 `--yes`；
- immutable rollback-spec v1 与单调一次性 pointer 已进入私有 content-addressed store，scope 固定为
  `same-controller-same-catalog-anchor-v1`；
- rollback 只接受同一主机、同一 controller、当前 Catalog 仍认可的 `latency` 锚点和本地
  artifact/image，不联网、不 pull、不 checkout Git；
- rollout 的 source/target quick 固定使用 `--no-record`，只作服务门禁，不生成 qualification。

existing-selection 恢复时，当前 free VRAM/RAM 会受到已选模型、runtime 和缓存占用影响，因而只作
advisory；它不能授权新部署，也不能绕过制品和配置身份。恢复后的健康、canonical Profile 与实际
容器身份始终是硬门槛。

上述结果关闭了 lifecycle-owner 风险并建立了可测试的 typed rollout 安全边界，但当前 Catalog 只有
一个 provisional LTS 条目，没有合法 validated rollback/LTS 模型对；真实命令因而按设计 fail
closed。下列工作仍是 Phase B 的完成条件：

- 把 `full` qualification 的 runner 记录、逐步结果和最终结论完整绑定到同一 rollout transaction；
- 在合法 validated rollback/LTS 模型对上完成一次 Tier-1 真实 upgrade→rollback drill，验证一次性
  pointer、失败注入与恢复结果；
- 完成一次真实 WSL shutdown/reboot、Docker 延迟就绪、systemd 自动恢复和活动事务重入验证；
- 在当前代码与配置上完成 host qualification，并生成可晋级的新证据。

这些条件未全部满足前，Phase C 明确禁止开始：不得删除旧 schema reader、candidate、兼容恢复
适配器或其他尚承担回滚职责的路径。

下面 Phase 0–7 记录前一轮加固的实现来源和仍需迁移的依赖，不再表示所有能力都会长期留在核心。

| Phase | 当前状态 | 说明 |
| --- | --- | --- |
| 0 基线与决策 | 首版完成 | ADR、行为边界、支持矩阵和 characterization tests 已落地 |
| 1 统一 CLI | 首版完成 | `./stack`、结构化结果、退出码和兼容适配已落地 |
| 2 类型化配置 | 加固完成，待实机复核 | Catalog 决定模型容量，Profile 只覆盖运行模式；schema v2 与矩阵测试已落地 |
| 3 持久事务 | typed rollout 基础已实现，full 绑定待收口 | recovery-required、事务 ID CAS、双锁、信号恢复、rollout intent/action journal、rollback spec/pointer 和 supervisor maintenance wait 已落地；full qualification 仍待统一纳入 |
| 4 证据晋级 | 门禁实现，尚无可晋级证据 | schema v4 绑定当前制品/完整安全信封；full、性能、受信签名和生命周期必须同时通过 |
| 5 最小权限运维 | 部分完成，跨仓库阻塞 | aggregate-only 快照和零凭据 Dashboard 已完成；专用 scope 待 ModelPort |
| 6 供应链与 bundle | 首版完成，持续强化 | 固定身份、来源审计和离线 bundle 已落地；签名覆盖继续扩展 |
| 7 校准/存储/凭据 | 部分完成 | baseline-only 校准、安全 GC、凭据审计/迁移已落地；性能阈值仍为 pending-baseline |

“首版完成”表示仓库已有可测试实现，不代表后续安全、兼容和跨主机证据工作结束。

## 执行规则

- 不进行一次性重写；新实现先通过兼容层接管旧命令。
- 每个阶段必须可独立发布、回滚和验收。
- 新 schema 先具备 reader、validator 和 migration check，再切换 writer。
- 运行状态改变仍需显式批准，路线图不会放宽 `readyToDeploy`。
- ModelPort 相关阶段必须在两个仓库联合评审和验收。

## Phase 0：基线与决策冻结

目标：在控制面重构前固定行为，防止无意改变现有安全语义。

交付物：

- ADR-0001 和本路线图；
- 当前命令、退出码、JSON 字段和本地文件清单；
- Linux 与 WSL2 的支持矩阵；
- 当前 quick/standard/full 覆盖矩阵；
- 关键黄金路径的 characterization tests。

完成条件：

- 现有 CLI 行为有机器可读快照；
- 所有本地可变状态都有所有者、权限、保留策略和恢复说明；
- 未记录的隐式兼容行为不进入后续迁移。

## Phase 1：统一 CLI 与 Python 控制面骨架

目标：提供稳定公共入口，不改变底层部署行为。

建议命令：

```text
stack plan
stack doctor
stack deploy
stack upgrade
stack rollback
stack status
stack verify
stack accept
stack release
stack storage
stack credentials
stack migrate
```

任务：

- 建立 `src/local_inference_stack/` Python package；
- 增加薄 `./stack` 启动器；
- 定义 command result schema、错误码和退出码；
- 旧脚本调用共享 package，或代理到新 CLI；
- 建立日志脱敏和统一 subprocess wrapper；
- 保证 `stack plan` 只依赖 Python 标准库。

完成条件：

- `stack plan/status/verify` 与旧命令结果等价；
- JSON contract 有兼容测试；
- 所有外部命令都有超时、结构化错误和受控环境；
- 首次只读规划在无 Docker daemon 情况下仍能给出可解释结果。

## Phase 2：类型化配置与派生文件

目标：减少 Catalog、Profile、Compose、Dashboard 和 manifest 的重复配置。

任务：

- 定义 `runtime-profiles` schema；
- 将 latency/throughput 参数迁移到类型化规范；
- 实现 `stack config render/check/diff`；
- 生成或校验 `.env` Profile 和 Dashboard baseline；
- manifest 生成器只读取权威来源和真实探针；
- 为 ModelPort contract 建立独立兼容映射，不单方面覆盖契约。

完成条件：

- 一个运行参数只存在于一个权威来源；
- 生成器幂等；
- 手工修改派生文件会使 CI 失败；
- 新旧配置渲染出的有效 Compose 身份一致。

## Phase 3：持久化事务与故障注入

目标：让部署和候选发布在进程或主机中断后仍可恢复。

核心生命周期状态：

```text
planned
-> production_stopping
-> candidate_starting
-> accepting
-> production_restoring
-> completed
```

upgrade/rollback 还在 transaction v2 中固定 source/target Catalog spec、rollback spec/pointer 和
有序 action plan；action ordinal/kind/subject 的授权与结果 journal 是状态名之外的权威进度。

任务：

- 定义事务 schema、原子写入和所有权规则；
- 为 deploy/profile/release/upgrade/rollback 建立共享状态机；
- 启动时优先 reconciliation；
- 建立 fake Docker/NVIDIA/curl/systemctl 测试环境；
- 对每个状态注入异常退出、超时、错误响应和 `SIGKILL` 后重入；
- 明确无法自动恢复时的 `recovery_required` 输出和人工 Runbook。

完成条件：

- 每个状态转换都有单元测试和集成测试；
- 任一失败点都不会同时遗留生产和候选占用稳定别名；
- 恢复后原 Profile、端口、镜像和挂载身份匹配；
- 未完成事务会阻止新的运行变更。

## Phase 4：证据与验证晋级

目标：区分本机运行证据和可复用验证证明。

任务：

- 定义 canonical reusable attestation schema；
- 记录 Git revision/dirty state、硬件、驱动、制品、镜像、配置和测试摘要；
- 建立签名接口，评估 Minisign 与 Sigstore/Cosign；
- Catalog `validated` 晋级只接受满足策略的 attestation；
- 建立撤销、过期和 supersede 语义；
- 旧 local acceptance 继续服务本机漂移检查。

完成条件：

- 自哈希 JSON 不能单独晋级 Catalog；
- 签名验证可以离线执行；
- 证据不包含主机原始身份、Prompt、回答或凭据；
- 修改测试依赖、配置或镜像会可靠使证据失效。

## Phase 5：ModelPort 最小权限运维契约

目标：让 Dashboard 不再持有管理员凭据。

任务：

- 与 ModelPort 定义版本化 aggregate operations API；
- 增加只读 operations scope 和专用凭据；
- Collector 以短生命周期运行并写入脱敏快照；
- Dashboard 只读取快照/SQLite；
- 契约包包含 schema、夹具和兼容测试；
- 移除对相邻 `MODELPORT_PROJECT_DIR` 的正式运行依赖。

完成条件：

- Dashboard 进程环境中不存在 ModelPort 凭据；
- Collector 凭据不能访问用户、密钥管理或原始请求内容；
- 两个仓库 CI 固定并验证同一契约版本；
- standard 验收覆盖权限拒绝和隐私字段。

## Phase 6：供应链证明与离线 bundle

目标：在固定身份之外记录来源、发布者和许可证审查状态。

任务：

- 定义 acquisition record；
- 固定并保存许可证摘要或快照；
- 在上游支持时验证容器签名；
- 实现 `stack bundle create/verify/import`；
- bundle 包含 Catalog 子集、制品、镜像引用、配置与证明；
- 导入仍需重新执行当前主机资源准入。

完成条件：

- 无网络主机可以完整验证 bundle；
- bundle 篡改在任何状态写入前被拒绝；
- identity/publisher/license 三种状态分别展示；
- 离线导入不会自动选择或启动模型。

## Phase 7：校准、存储与凭据生命周期

目标：补齐长期使用能力，但不牺牲确定性。

任务：

- `stack calibrate` 生成候选 Profile 和对比报告；
- `stack storage report/gc --dry-run` 建立引用图；
- 固定当前生产、事务和回滚锚点；
- 增加 systemd credentials/本机密钥环 backend；
- `.env` 提供只读审计和显式迁移；
- 生成配置、退出码和 CLI reference 文档。

完成条件：

- calibrate 不能直接修改生产 Profile；
- GC 默认不删除，且输出精确路径、大小和保留理由；
- 删除不会破坏当前部署、未完成事务或最近回滚；
- Dashboard、日志和证据始终无法访问凭据内容。

## 持续工作：文档、弃用与质量

- 维护 Quickstart、学习指南、Operator Runbook 和 Contributor Reference 四层文档；
- 从代码生成 CLI/schema/错误码 reference；
- 旧脚本至少保留一个明确版本周期的弃用提示；
- Linux 与 WSL2 分别收集验收和恢复证据；
- 每个 Phase 完成后更新 ADR 状态、路线图和 deployment manifest；
- 新硬件 backend、在线自适应或 Dashboard 写操作仍需独立 ADR、威胁建模和验收，不能因
  Phase 0–4 已有首版实现而自动扩大可信边界。

## 首个实施切片（已完成）

已交付的第一个代码切片为：

1. 建立 Python package 和 `./stack`；
2. 实现只读 `stack plan --json`；
3. 定义统一结果与退出码；
4. 用 characterization tests 证明它与现有 `model-manager.py plan --json` 等价；
5. 不改下载、选择、运行或验收路径。

当前后续优先级不是重新实现该切片，而是：

1. 在明确批准的维护窗口完成至少三轮 `baseline-only` 校准，评审后写入硬阈值；
2. 在干净提交上运行 schema v4 `full`，复核逐步结果、制品和完整容器安全信封，再用固定的
   受信公钥策略签名；
3. 将 `full` qualification runner 的完整记录与结论绑定到 typed rollout transaction；现有
   `quick --no-record` 只保留为服务门禁，不能冒充 qualification；
4. 与 ModelPort 约定 provider self-attestation/health contract，并把 operations、数据库、
   Dashboard、备份和管理员凭据代码迁出核心；
5. 解除 Catalog admission 对 reusable attestation/offline bundle 的依赖，再把两者迁为可选
   发布工具或删除；核心只保留签名 Git release、固定 artifact identity 和本机 qualification；
6. 在合法 validated rollback/LTS 模型对上完成 Tier-1 真实 upgrade→rollback，再补齐 WSL2 /
   RTX 5070 Ti shutdown/reboot、Docker 延迟就绪和失败恢复证据；
   native Linux 在有真实主机证据前保持 Tier-2 只读诊断。上述 Phase B 门槛关闭前不启动 Phase C。

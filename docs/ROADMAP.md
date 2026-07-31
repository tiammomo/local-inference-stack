# 项目优化路线图

本路线图落实 [ADR-0001](decisions/0001-trusted-single-host-appliance.md)。它描述依赖顺序和完成定义，
不是承诺日期。任何阶段都必须保持现有安全黄金路径可用。

截至 2026-07-31，本仓库侧 Phase 0–4、6–7 已形成首个可用实现；Phase 5 已完成零凭据
Dashboard、短生命周期 Collector 和版本化快照契约。本仓库无法单方面创建 ModelPort 的最小权限
operations scope，该项必须在 ModelPort 仓库联合实现后才能宣告端到端完成。

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

事务状态建议：

```text
planned
-> production_stopping
-> candidate_starting
-> accepting
-> production_restoring
-> completed
```

任务：

- 定义事务 schema、原子写入和所有权规则；
- 为 deploy/profile/release 建立共享状态机；
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
- 在 Phase 0–4 完成前，不扩展新硬件 backend、在线自适应或 Dashboard 写操作。

## 推荐的首个实施切片

第一个代码切片应严格限制为：

1. 建立 Python package 和 `./stack`；
2. 实现只读 `stack plan --json`；
3. 定义统一结果与退出码；
4. 用 characterization tests 证明它与现有 `model-manager.py plan --json` 等价；
5. 不改下载、选择、运行或验收路径。

这个切片风险最低，也为后续配置和事务工作提供稳定边界。

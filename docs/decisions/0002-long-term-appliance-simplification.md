# ADR-0002：长期单机 Appliance 收口与兼容终止

- 状态：Accepted
- 日期：2026-08-09
- 决策者：项目维护者
- 取代范围：细化 ADR-0001 的长期产品边界和迁移终态；不降低其安全约束

## 背景

项目已经具备保守规划、制品完整性、llama.cpp runtime、持久事务、验收和恢复能力，
但仍处于“新 Python 控制面包裹旧 Python/Shell 脚本”的迁移中间态。仓库拥有大量公共
脚本、重复探针、重复文件摘要、历史兼容 reader，以及超出本仓库所有权的 ModelPort
内部运维验证。复杂度已经开始妨碍判断真正的权威来源。

维护者确认未来 2–3 年的目标不是面向陌生用户和未知硬件的通用推理平台，而是服务少量
自有、可信、单用户 NVIDIA 工作站的长期本地推理 Appliance。精简的目标是减少重复所有权
和不可证明的支持承诺，不是删除恢复、完整性或批准边界。

## 决策

### 1. 产品与支持范围

- WSL2 x86_64、单 NVIDIA GPU 和 llama.cpp/GGUF 是 Tier 1 方向；当前 RTX 5070 Ti
  16 GB 主机是首个必须重新取得当前证据的认证环境。
- native Linux 是 Tier 2 兼容方向；没有真实主机 qualification 时只提供只读诊断。
- 不建设多 backend、集群、多节点、多租户、共享 GPU、零停机或通用网关抽象。
- 没有维护人、真实硬件和周期性复验的配置不得出现在可执行 Catalog。

### 2. 模型生命周期

可执行 Catalog 只表达部署 allowlist，而不是模型商城。目标状态最多包含：

- 一个经过当前 qualification 的 LTS 模型；
- 一个仍可验证并恢复的回滚锚点；
- 一个隔离、不可直接生产部署的候选。

未经真实硬件验证的估算条目移出可执行 Catalog。`lifecycleRole` 只表示槽位职责，与
evidence status 正交。当前 Qwen3.5-9B 占用 `lts` 槽但仍保持 provisional；在完成新
qualification 前不得被描述为 production-qualified LTS，也不得自动部署。

### 3. 核心仓库所有权

本仓库长期拥有：

- Catalog、模型来源、GGUF 字节数与 SHA256；
- 主机探测、准入和确定性 llama.cpp `CatalogDeploymentSpec`；
- 单 runtime 的部署、升级、回滚、持久事务与实际身份验证；
- 本机健康、质量、上下文和硬件绑定性能 qualification；
- 一个稳定的 `./stack` 公共入口；
- 一个极薄、版本化的 ModelPort provider contract/health adapter。

ModelPort、Postgres、Dashboard、管理员凭据、数据库备份、Collector、报表和告警不再属于
核心仓库。它们迁回 ModelPort 或独立 ops pack；本仓库不长期验证其内部 Compose、容器、
环境变量或数据库实现。

### 4. 单一 runtime 与生命周期 owner

- 生产始终只有一个 llama.cpp runtime。
- systemd user supervisor 是目标状态下唯一的启动、等待 Docker、监控和恢复 owner。
- Docker 自身不自动重启 runtime，手工 `docker compose up` 不属于支持路径。
- 升级接受明确的短维护窗口：在线预取并校验，停产后启动候选配置，qualification 成功则
  提升，失败则恢复已验证回滚锚点。
- candidate port、candidate profile 和第二套 Shell release/recovery 状态机在迁移完成后删除。

### 5. 中性控制面

Qwen3.5 是当前模型选择，不是基础设施类型。目标状态将 `QWEN_*`、固定 qwen 容器名和
单一 deployment 路径迁移为中性的 `INFERENCE_*`、`local-inference-runtime` 和类型化
`CatalogDeploymentSpec`。该 spec 绑定完整受审 Catalog model 及其 artifact/runtime
投影，但不冒充 image、Compose、网络、端口和主机策略组成的完整 effective runtime identity。
该中性化不引入 runtime backend 插件层；核心仍只支持 llama.cpp/GGUF。

### 6. 验收与信任

目标验收面收敛为：

- `stack check`：只读检查配置、制品、事务和实际 runtime 身份；
- `stack qualify`：在维护窗口执行本机生成、上下文、质量和性能门禁，生成限时主机记录；
- `stack check integration modelport`：可选验证 provider contract、握手和 ModelPort 自证材料。

一台主机的 qualification 不能授权另一台主机。长期发布信任由签名 Git release、Catalog
固定制品身份和每台主机独立 qualification 共同构成。没有已确认的隔离网分发需求，因此离线
bundle 和跨团队 reusable attestation 迁出核心控制面；删除前必须先解除当前 admission 依赖。

当前 deploy lifecycle 中的 `quick-smoke` 只验证启动后的最小安全路径，不生成或宣称 host
qualification；完整 qualification 仍只属于维护窗口中的 `stack qualify` 目标命令。

### 7. 公共 CLI

迁移终态只承诺以下生命周期入口：

```text
stack plan
stack deploy
stack upgrade
stack rollback
stack status
stack check
stack qualify
stack doctor
stack storage
```

旧 `verify/profile/release/accept/calibrate` 能力合并到上述生命周期；
`bundle/attest/credentials` 退出核心；`config/migrate/reference` 仅为开发或迁移内部工具。
普通用户不直接依赖 `scripts/`。该列表是迁移目标，不表示当前阶段已删除既有命令。

### 8. 兼容终止

项目接受一次受控的破坏性升级。完成支持主机盘点、历史导出、现场 v1 事务处理和回滚验证后：

- 只承诺 `./stack` 和当前 schema；
- 删除 transaction/runtime-profile/bundle v1 reader 与旧脚本 wrapper；
- 历史 deployment/evidence 作为不可变归档保留，但不再承担当前配置职责；
- Git 历史和明确迁移记录承担旧实现审计，不在生产路径永久保存兼容分支。

当前主机仍有 schema-v1 failed transaction，因此本条决策不授权现在直接删除 v1
reconciliation 路径，也不授权无批准地修改运行状态。

### 9. 不可精简的安全边界

以下复杂度属于长期核心，不以减少行数为理由删除：

- `--yes`、Catalog admission 和 `readyToDeploy=false` 硬停止；
- 持久事务、transaction ID CAS、全局锁序、orphan fence 和原 runtime 回滚；
- dirfd、`O_NOFOLLOW`、owner/mode/nlink、原子替换以及文件和目录 fsync；
- 模型精确字节数和 SHA256；
- 实际 runtime 镜像、命令、环境、网络、挂载和端口身份；
- loopback 与 least-privilege 容器边界；
- secret-free evidence、受控 subprocess 环境、超时和结构化错误。

## 迁移计划

迁移必须按三个阶段完成：

1. **旁路重构**：建立 package-native domain/application/adapters、统一 RuntimeObservation 和
   MaterialSet，保持运行实例和外部安全行为不变。
2. **维护窗口切换**：经单独批准后处理 v1 事务，切换中性配置、唯一 supervisor 和新
   `CatalogDeploymentSpec`，并执行当前主机 qualification。
3. **验证后删除**：证明新 LTS、回滚、主机重启和失败恢复后，删除旧 schema、wrapper、
   candidate、operations、bundle 和 attestation 路径。

阶段 1 不得借重构名义启停容器、重写本机 evidence、签名或处理持久事务。

## 可量化目标

- package 不再通过 subprocess 或动态 import 调用仓库内 Python 脚本；
- 内部 planner 不再传递 Shell 命令字符串，而使用类型化 action；
- canonical file/material digest 只有一个实现；
- 可执行脚本入口不超过 15 个，Shell 总量不超过约 400 行；
- 核心生产代码净减至少 20%，完成 operations 迁出后争取约 40%；
- 公共 runtime 只有一个生命周期 owner；
- 删除代码不能降低故障注入、完整性、回滚或权限负测试覆盖。

## 影响

正面影响是权威来源更少、支持承诺更诚实、故障恢复更容易推理，新 LTS 模型不再要求修改
Qwen 专用基础设施。代价是一次明确的不兼容迁移、ModelPort 双仓库协同、维护窗口，以及在
新 qualification 完成前继续保持 Catalog fail-closed。

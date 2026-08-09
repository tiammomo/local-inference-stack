# 控制面参考（生成文件）

> 由 `./stack reference --write --yes` 生成；不要手工编辑。

全局 `--json` 可以放在命令前后。结构化输出和退出码稳定；命令是否改变本地状态、
是否需要批准，以各节为准。`scripts/` 不构成公共契约；普通生命周期不要直接调用，只有明确列出它的运维或贡献者 Runbook 例外。

## 顶层命令

- `./stack plan` — 只读评估硬件并给出 Catalog 推荐。
- `./stack doctor` — 检查平台、依赖、配置和待恢复事务。
- `./stack status` — 报告 runtime 与持久事务状态。
- `./stack verify` — 在不改变运行状态的前提下验证仓库、配置或模型。
- `./stack deploy` — 取得 --yes 批准后解释绑定 Catalog identity 的类型化部署计划。
- `./stack accept` — 取得 --yes 批准后运行指定验收层级。
- `./stack release` — 取得 --yes 批准后执行串行候选发布与生产恢复。
- `./stack profile` — 通过持久事务切换固定生产 Profile。
- `./stack reconcile` — 查看或显式修复未完成事务。
- `./stack config` — 校验或确定性渲染运行配置。
- `./stack attest` — 创建、签名或验证可复用验收证明。
- `./stack bundle` — 创建、验证或显式导入离线 bundle。
- `./stack calibrate` — 规划或运行只生成报告的本机校准。
- `./stack storage` — 报告存储或保守清理临时文件。
- `./stack credentials` — 审计凭据元数据或显式迁移凭据。
- `./stack migrate` — 检查 schema 兼容性，不静默迁移。
- `./stack reference` — 从控制面定义生成本参考。

## 完整语法

### `./stack plan`

探测或模拟主机容量，并返回 Catalog 推荐、证据状态和下一步。容量覆盖只用于模拟。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack plan [--model CATALOG_ID] [--vram-gib GIB] [--ram-gib GIB] [--json]`
- 示例：`./stack plan --json`

### `./stack doctor`

检查平台、依赖、配置漂移和未完成事务；不启动服务。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack doctor [--json]`
- 示例：`./stack doctor --json`

### `./stack status`

按 scope 报告 runtime、持久事务和可选联合运维组件；默认只检查 standalone。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack status [--scope {standalone,integrated,all}] [--json]`
- 示例：`./stack status --json`

### `./stack verify`

验证仓库、类型化配置、Catalog 制品或指定范围的部署组件。

- 状态：只读；可能执行本地测试或模型哈希读取。
- 批准：不需要。
- 语法：`./stack verify [--scope {repository,config,model,standalone,integrated,all}] [--model CATALOG_ID] [--cached] [--json]`
- 示例：`./stack verify --scope config --json`

### `./stack deploy`

只解释当前只读 plan 返回且与 Catalog identity 绑定的类型化部署步骤。

- 状态：写入并启动 runtime；可能下载制品。
- 批准：必须 --yes，且 plan 必须准入。
- 语法：`./stack deploy [--model CATALOG_ID] --yes [--json]`
- 示例：`./stack deploy --model qwen35-9b-q5km --yes`

### `./stack accept`

运行指定验收层级；standard/full 还需要显式 ModelPort 环境。

- 状态：运行测试负载并写验收证据。
- 批准：必须 --yes。
- 语法：`./stack accept [{quick,standard,full}] --yes [--json]`
- 示例：`./stack accept quick --yes`

### `./stack release`

在单 GPU 上串行执行候选验收，始终校验生产恢复身份。

- 状态：停止生产、运行候选并恢复生产。
- 批准：必须 --yes。
- 语法：`./stack release [{quick,long}] --yes [--json]`
- 示例：`MODELPORT_PROJECT_DIR=/path/to/ModelPort ./stack release quick --yes`

### `./stack profile`

通过持久事务切换固定生产 Profile。

- 状态：重建 runtime。
- 批准：必须 --yes。
- 语法：`./stack profile {latency,throughput} --yes [--json]`
- 示例：`./stack profile throughput --yes`

### `./stack reconcile`

查看或修复中断的持久运行事务。

- 状态：默认只读；--yes 执行恢复。
- 批准：修复时必须 --yes。
- 语法：`./stack reconcile [--yes] [--json]`
- 示例：`./stack reconcile --json`

### `./stack config check`

校验类型化配置及其派生 Profile、Compose 和 Dashboard 文件。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack config check [--json]`
- 示例：`./stack config check --json`

### `./stack config render`

确定性渲染类型化配置；无 --write 时只返回内容。

- 状态：默认只渲染；--write 写派生文件。
- 批准：写入时必须 --yes。
- 语法：`./stack config render [--write --yes] [--json]`
- 示例：`./stack config render --json`

### `./stack attest create`

从本机验收证据创建不含凭据的可复用 attestation 草稿。

- 状态：写显式 --output 文件。
- 批准：输出路径即显式目标。
- 语法：`./stack attest create --evidence PATH --output PATH [--json]`
- 示例：`./stack attest create --evidence logs/acceptance/FILE.json --output attestation.json`

### `./stack attest sign`

对已审阅 attestation 生成外部签名；私钥内容不进入结果。

- 状态：调用签名工具并写签名文件。
- 批准：必须 --yes。
- 语法：`./stack attest sign PATH --tool {minisign,cosign} --secret-key PATH --signature PATH --yes [--json]`
- 示例：`./stack attest sign attestation.json --tool minisign --secret-key KEY --signature attestation.minisig --yes`

### `./stack attest verify`

校验结构、自哈希和生命周期；分离签名必须显式选择密码学验证，Catalog 晋级还必须用外部固定公钥指纹重检当前输入。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack attest verify PATH [--require-signature] [--for-promotion --trusted-key-sha256 SHA256] [--tool {minisign,cosign} --public-key PATH --signature PATH] [--json]`
- 示例：`./stack attest verify attestation.json --for-promotion --tool minisign --public-key KEY.pub --signature attestation.minisig --trusted-key-sha256 SHA256`

### `./stack bundle create`

仅为 LTS 生命周期条目创建带成员清单、大小和哈希的离线复现 bundle。

- 状态：写离线 bundle；可包含大制品。
- 批准：必须 --yes。
- 语法：`./stack bundle create --model CATALOG_ID --output PATH [--include-model] [--image-archive PATH] --yes [--json]`
- 示例：`./stack bundle create --model qwen35-9b-q5km --output stack.tar --yes`

### `./stack bundle verify`

在解包或写入前验证 bundle 路径、成员、大小和哈希。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack bundle verify PATH [--json]`
- 示例：`./stack bundle verify stack.tar --json`

### `./stack bundle import`

原子导入已验证制品，之后仍需重新运行主机准入。

- 状态：导入匹配 Catalog 的制品；不选择或启动。
- 批准：必须 --yes。
- 语法：`./stack bundle import PATH --yes [--json]`
- 示例：`./stack bundle import stack.tar --yes`

### `./stack calibrate plan`

显示离线候选 Profile 校准计划，不改生产。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack calibrate plan [--json]`
- 示例：`./stack calibrate plan --json`

### `./stack calibrate run`

执行报告型校准，结果需人工评审后才能进入 Profile。

- 状态：运行本地探针并写候选报告；不改生产。
- 批准：必须 --yes。
- 语法：`./stack calibrate run [--output PATH] --yes [--json]`
- 示例：`./stack calibrate run --output calibration.json --yes`

### `./stack storage report`

报告 models、cache、logs、backups 占用和保护引用。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack storage report [--json]`
- 示例：`./stack storage report --json`

### `./stack storage gc`

只处理超龄且未被生产、事务或回滚锚点引用的 .part/.tmp。

- 状态：默认 dry-run；--yes 删除受限临时文件。
- 批准：删除时必须 --yes。
- 语法：`./stack storage gc [--older-than-days DAYS] [--yes] [--json]`
- 示例：`./stack storage gc --older-than-days 14 --json`

### `./stack credentials audit`

只检查凭据路径、类型和权限，不读取或输出值。

- 状态：只读元数据。
- 批准：不需要。
- 语法：`./stack credentials audit [--json]`
- 示例：`./stack credentials audit --json`

### `./stack credentials migrate-systemd`

把选定本地凭据显式迁移到 systemd credential backend。

- 状态：写 systemd credential；保留兼容源。
- 批准：必须 --yes。
- 语法：`./stack credentials migrate-systemd {operations,backup,alerting} --yes [--json]`
- 示例：`./stack credentials migrate-systemd operations --yes`

### `./stack migrate`

按各 schema 的显式可读集合检查兼容性，只报告、不静默改写。

- 状态：只读。
- 批准：不需要。
- 语法：`./stack migrate [--check] [--json]`
- 示例：`./stack migrate --check --json`

### `./stack reference`

渲染或校验本页；CLI 变更后应重新生成。

- 状态：默认渲染；--check 只读；--write 更新生成文档。
- 批准：写入时必须 --yes。
- 语法：`./stack reference [--check | --write --yes] [--json]`
- 示例：`./stack reference --check --json`

## 稳定退出码

- `0` — 成功
- `2` — 参数或显式批准错误
- `3` — 主机/资源准入拒绝
- `4` — 配置或 schema 错误
- `5` — 外部命令、服务或网络错误
- `6` — 哈希、签名或 bundle 完整性错误
- `7` — 存在未恢复事务或恢复失败

## 结构化结果 schema

所有带 `--json` 的命令输出一个对象：

```json
{"schemaVersion":1,"command":"plan","status":"ok","code":0,"summary":"...","facts":{},"nextActions":[]}
```

`nextActions[].requiresApproval=true` 表示该动作会改变主机状态。

## 本地 schema 版本

- `attestation`: 当前 `2`（仅 v2 可读；v1 缺少当前信任绑定，拒绝读取）
- `bundle`: 当前 `2`（可读 v1/v2；v1 仅支持纯制品，未绑定镜像 archive 必须重建）
- `commandResult`: 当前 `1`（仅 v1 可读）
- `runtimeProfiles`: 当前 `2`（可读 v1/v2；v1 只读检查）
- `transaction`: 当前 `2`（可读 v1/v2；v1 必须分类并经显式 reconcile 处理）

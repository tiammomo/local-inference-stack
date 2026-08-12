# 升级与回滚

升级的目标是保留不可变来源、当前可用 runtime 和可解释的回滚点。不要把 Git 更新、模型下载、
Profile 切换和服务重建合并成一个未经评审的动作。

当前 Tier-1 主机已经完成独立的 owner-migration 维护切片：历史 v1 事务显式解析、私有 selection
规范化、Docker restart policy 迁移为 `no`，并通过“停止 runtime → systemd supervisor 恢复 →
post-start 健康与实际身份检查”演练。仓库也已具备类型化 upgrade/rollback 的控制面基础，但尚未
在真实 LTS/rollback 模型对上完成 rollout、重启与 full qualification 演练。因此这不是 Phase B
完成或新的 qualification 结论。

## 1. 升级前准备（不改变 runtime）

```bash
git status --short
git fetch --prune
./stack plan --json
./stack status --json
./stack migrate --check --json
./stack config check --json
./stack storage report --json
```

先审阅上游提交、当前 dirty 文件、schema 变化、Catalog revision、镜像 digest、Profile 和文档。
`git fetch --prune` 会更新本地 remote-tracking refs，但不会合并或覆盖工作树；需要完全离线审阅时可
跳过它并使用已有 refs。
`readyToDeploy=false` 在健康 runtime 已占用 GPU 时很常见；它仍然禁止再次部署，不能作为绕过
准入的理由。更新 Git 工作树的合并或 rebase 策略由维护者决定，本仓库不自动覆盖本地改动。

恢复同一 immutable selection 时，当前 free VRAM/RAM 只作 advisory：运行中的模型和缓存本身会
降低这些数值，不能用面向新部署的即时余量再次否定已有选择。类型化 replacement 准入也只把
当前空闲量视为 advisory，因为源 runtime 会先停止；总容量、精确 Tier-1 硬件、运行前置条件、
Catalog 信任和目标 LTS 资格仍是硬门槛。这两个例外都不允许并行启动第二个模型。启动后的
`/health`、`latency` Profile、实际制品和容器身份必须全部通过，否则进入恢复流程。

## 2. 按变化类型选择验证

| 变化 | 最低验证 |
| --- | --- |
| 文档或控制面 | 单测、文档链接/命令检查、代表性 plan、Compose 渲染、quick |
| Profile、Compose、运行脚本 | 上述检查 + 当前 Profile 身份 + quick |
| ModelPort、reasoning、Token、Tool Use | `standard` |
| 模型、GGUF、KV、上下文、镜像或性能基线 | `full`、供应链复核和新 deployment 证据 |

```bash
./scripts/release-check.sh
./scripts/model-manager.py plan --vram-gib 8 --ram-gib 32 --json
./scripts/model-manager.py plan --vram-gib 16 --ram-gib 64 --json
./scripts/model-manager.py plan --vram-gib 24 --ram-gib 64 --json
./scripts/acceptance-suite.sh quick
```

这里的独立 quick 可以为当前主机写入 schema v4 evidence。upgrade/rollback 事务内部运行的是
`quick --no-record`：它只回答切换前后服务是否可用，不写 host acceptance，不构成 qualification，
也不能晋级 Catalog。

公共 CLI 改变后重新生成并校验参考文档：

```bash
./stack reference --write --yes
./stack reference --check --json
python3 scripts/check-doc-commands.py
```

## 3. 模型或运行配置升级

模型、GGUF 和镜像只能来自经过评审的 Catalog/Compose 固定身份。记录许可证来源、不可变 revision、
精确字节数、SHA256 和镜像 digest；哈希只证明身份，不证明发布者或许可证适用性。

16GB 单卡不能同时驻留完整生产和目标。正式 replacement 必须使用公共类型化入口；不带 `--yes`
的 upgrade 只做 source/target 准入预检，rollback 还会验证 active pointer 和 action plan。两者都不
创建事务、不写 rollback store，也不改变 runtime：

```bash
./stack upgrade --model CATALOG_ID
./stack upgrade --model CATALOG_ID --yes

./stack rollback
./stack rollback --yes
```

`--yes` 只表示批准已显示的维护窗口，不会放宽准入。upgrade 要求：

- 当前 selection 是精确的 Catalog 投影，运行于 `latency`，健康且具有当前主机的安全 evidence；
- 源条目是带可信 full attestation 的 `validated` rollback 条目，目标是带可信 full attestation
  的不同 `validated` LTS 条目；
- 主机仍是精确 Tier-1 WSL2 x86_64 档案，Catalog、Compose、控制器材料和制品身份没有漂移；
- 目标制品只通过绑定到下一类型化 action 的 Catalog 下载路径取得，不能绕过 action ordinal、
  subject、kind 或 transaction ID。

执行时，控制面在 runtime/transaction 双锁内保存源的 immutable rollback-spec v1 和旧 pointer，
然后按持久计划依次运行源 quick、获取目标制品、停止源、选择并启动目标、运行目标 quick，最后才
复核源锚点并发布一次性 rollback pointer。每一步结果摘要和下一步 action 都由事务 CAS 绑定；
失败或中断进入 `recovery_required`，不能开始另一次变更。

rollback-spec v1 的 scope 固定为 `same-controller-same-catalog-anchor-v1`。它只允许同一可信主机、
同一精确控制器材料集合、当前 Catalog 中同一 rollback anchor、`latency` Profile，以及已经存在于
本地且重新验证通过的 GGUF 和固定镜像 ID。`stack rollback` 不联网、不下载、不执行 image pull，
也不 checkout Git；若控制器 revision/material、Catalog、主机、evidence、制品、Compose、镜像或
pointer 任一不匹配，就 fail closed。Git 或其他前置状态的恢复必须作为单独评审动作完成，不能让
rollback 命令自行改写工作树。

rollback 在同一事务中停止当前源、写入已验证锚点 selection、启动 `latency`、运行
`quick --no-record`，成功后清除 active pointer。pointer 是一次性的；已消费、缺失或 tombstoned
的 pointer 不能再次回滚。恢复不从“当前看起来健康”的可变状态推断锚点：upgrade 失败恢复升级前
的源锚点；rollback 已经把持久锚点定义为恢复目标，因此 rollback 失败会继续恢复并验证同一锚点，
而不是重新启用调用前的待替换 runtime。两种路径都只恢复事务记录的 pointer 前驱。

当前可执行 Catalog 只有一个 `provisional` 条目，既没有可信的 validated LTS/rollback
模型对，也不能形成不同的 upgrade source/target；新安装上也没有合法 active rollback pointer。
因此当前真实 upgrade/rollback 会在准入阶段 fail closed。这是预期行为，不能通过手写 Catalog、
evidence、pointer 或 selection 绕过。

旧 `release`/candidate 流程仍用于兼容性候选检查，不是正式 upgrade 的替代入口。只有目标主机
完成对应独立 `full` qualification 后，才能更新 Catalog 状态或 deployment manifest；估算条目
不能仅凭相似硬件或事务内 quick 晋级。

## 4. schema、生成文件和 manifest

兼容范围按 schema 单独声明，不笼统承诺 N-1。runtime Profile 与 transaction 当前为 v2；类型化
upgrade/rollback 在 v2 transaction 中保存 rollout intent、精确 action plan、逐步结果和 action
ordinal。rollback spec 与单调 pointer 各为 v1，保存在私有 content-addressed store 中。当前
Tier-1 主机的历史 v1 transaction 已经显式解析，但通用 v1 reader 在 Phase B 完成前仍须保留。
其他主机遇到旧 `failed` transaction 仍必须先分类当前 runtime，不能自动改写为安全终态。bundle 当前
为 v2：v1 的纯制品 bundle 可只读验证/导入，带未绑定镜像 archive 的 v1 bundle 必须用当前工具重建；
attestation v1 因缺少当前信任与输入绑定而拒绝读取。先运行 `stack migrate --check` 并审阅报告；
该命令只审计、不写迁移。v1 transaction 的现场处理入口是经批准的 `stack reconcile --yes`；修改
权威运行配置后，才用 `stack config render --write --yes` 更新派生文件。
终态 transaction 会在单槽当前指针被下一事务替换前写入私有逐事务归档；升级前应确认归档目录
为当前用户所有、`0700`，文件为 `0600`。对归档策略上线前已经丢失的终态，只能保留带
`resolutionTimestampAvailable=false` 等限制说明的 migration ledger，不得反推或补造时间。

deployment manifest 把两类哈希分开：`validatedConfiguration` 永久保留上次实机验收时的历史输入，
`repositoryConfiguration` 跟踪当前提交的静态文件。代码变化后可以机械刷新后者，但这绝不改变
`validation.status`、Catalog 资格或历史证据；只有完成对应实机验收和受信签名后，才能建立新的
validated 集合并晋级。随后运行：

```bash
python3 scripts/verify-manifest.py --json
./stack verify --scope all --json
```

`--scope standalone` 不要求 ModelPort，但会验证实际容器完整安全信封、活动制品和控制事务；
`integrated`/`all` 才要求清单声明的
ModelPort、operations、备份与联合身份，不把“未配置”和 daemon/user-bus 故障混为一谈。

## 5. 回滚

计划回滚时先运行 `./stack rollback`，审阅 active pointer、精确 source/target 和类型化 action
plan，再在维护窗口批准 `./stack rollback --yes`。若 upgrade/rollback 事务已经中断，不要新建
第二个 rollback 事务；先只读运行 `./stack reconcile --json`，按其恢复计划取得批准后执行
`./stack reconcile --yes`。不要删除旧 GGUF、镜像、evidence 或 rollback object，直到事务进入
验证终态。

rollback 恢复的是同一控制器和当前 Catalog 仍认可的精确锚点，不会恢复 Git，不会让过期、复制
或不匹配的 host acceptance 自动有效。涉及安全事件、凭据泄露或不可信制品时，还要轮换凭据、
撤销相关证据并按 [SECURITY.md](../SECURITY.md) 的私密渠道处理；不要把普通 availability rollback
当成安全事件处置。

Phase B 仍未完成。当前实现已把类型化 plan、锚点、action authorization、quick 服务门禁和失败
恢复接入持久事务，但还需要把 `full` qualification 的 runner 记录和结论完整绑定到同一 rollout
transaction，并在具备合法 validated LTS/rollback 模型对后完成一次 Tier-1 真实 upgrade→rollback
演练。真实 WSL shutdown/reboot、Docker 延迟就绪和事务恢复 drill，以及当前代码/config 的 full
host qualification 也仍待完成。这些门槛关闭前禁止开始 Phase C，不能删除仍承担 schema、candidate
或恢复职责的兼容路径。

# 升级与回滚

升级的目标是保留不可变来源、当前可用 runtime 和可解释的回滚点。不要把 Git 更新、模型下载、
Profile 切换和服务重建合并成一个未经评审的动作。

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

公共 CLI 改变后重新生成并校验参考文档：

```bash
./stack reference --write --yes
./stack reference --check --json
python3 scripts/check-doc-commands.py
```

## 3. 模型或运行配置升级

模型、GGUF 和镜像只能来自经过评审的 Catalog/Compose 固定身份。记录许可证来源、不可变 revision、
精确字节数、SHA256 和镜像 digest；哈希只证明身份，不证明发布者或许可证适用性。

16GB 单卡不能同时驻留完整生产和候选。需要停产验证时，先取得维护窗口批准，再使用受事务保护的
串行候选流程；不要手工启动第二个占用同一 GPU 的实例：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./stack release quick --yes
```

只有目标主机通过相应验收后，才能更新 Catalog 状态或 deployment manifest。估算条目不能仅凭相似
硬件升级为 validated。

## 4. schema、生成文件和 manifest

兼容范围按 schema 单独声明，不笼统承诺 N-1。runtime Profile 与 transaction 当前为 v2，v1
只读保留；旧 `failed` transaction 必须先分类当前 runtime，不能自动改写为安全终态。bundle 当前
为 v2：v1 的纯制品 bundle 可只读验证/导入，带未绑定镜像 archive 的 v1 bundle 必须用当前工具重建；
attestation v1 因缺少当前信任与输入绑定而拒绝读取。先运行 `stack migrate --check` 并审阅报告；
该命令只审计、不写迁移。v1 transaction 的现场处理入口是经批准的 `stack reconcile --yes`；修改
权威运行配置后，才用 `stack config render --write --yes` 更新派生文件。

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

回滚前记录失败步骤和当前事务，保留旧 Git revision、镜像 digest、Catalog 制品、Profile 与最近
可用验收证据。若事务未完成，先只读运行 `./stack reconcile --json`，按其恢复计划取得批准后再执行
`./stack reconcile --yes`。不要删除旧 GGUF、镜像或备份，直到生产恢复且 quick 通过。

回到已审阅版本后重新检查配置、启动身份、健康和 quick；回滚得到的是上一已知配置，不会让过期、
复制或不匹配的 host acceptance 自动恢复有效。涉及安全事件、凭据泄露或不可信制品时，还要轮换
凭据、撤销相关证据并按 [SECURITY.md](../SECURITY.md) 的私密渠道处理。

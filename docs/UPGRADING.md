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

本地 schema 只读兼容 N-1，不允许静默迁移。先运行 `stack migrate --check`，再评审 reader、validator、
migration check 和 writer 的顺序。修改权威运行配置后，用 `stack config render --write --yes` 更新派生文件。

deployment manifest 跟踪关键配置和验证脚本的 SHA256。只有在变化经过评审与对应验收后，才更新其
digest；随后运行：

```bash
python3 scripts/verify-manifest.py --json
python3 scripts/verify-deployment.py
```

`verify-deployment.py` 需要清单声明的 runtime、ModelPort 和 operations 服务均存在；仅 standalone
运行时使用 quick 与 manifest 文件哈希检查，不伪造缺失的联合运维状态。

## 5. 回滚

回滚前记录失败步骤和当前事务，保留旧 Git revision、镜像 digest、Catalog 制品、Profile 与最近
可用验收证据。若事务未完成，先只读运行 `./stack reconcile --json`，按其恢复计划取得批准后再执行
`./stack reconcile --yes`。不要删除旧 GGUF、镜像或备份，直到生产恢复且 quick 通过。

回到已审阅版本后重新检查配置、启动身份、健康和 quick；回滚得到的是上一已知配置，不会让过期、
复制或不匹配的 host acceptance 自动恢复有效。涉及安全事件、凭据泄露或不可信制品时，还要轮换
凭据、撤销相关证据并按 [SECURITY.md](../SECURITY.md) 的私密渠道处理。

# 验收与发布

## 验收分层

| 模式 | 覆盖 | 何时运行 |
| --- | --- | --- |
| `quick` | 单测、活动权重、运行态、直连生成与思考 | 首次部署、普通本仓变更 |
| `standard` | quick + ModelPort 契约、Token、Dashboard、Tool Use、质量冒烟 | 协议、推理或 Tool 变更 |
| `full` | standard + 完整哈希、长上下文、性能、完整质量集 | 模型、量化、KV、上下文、镜像升级 |

```bash
./scripts/acceptance-suite.sh quick
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard
MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh full
```

所有模式 fail-fast。`standard/full` 只支持当前版本化的 9B Provider 契约，并要求显式
提供兼容 ModelPort checkout。

## 本机凭证

验收日志和 JSON 写入 Git 忽略的 `logs/acceptance/`。只有通过的 schema v3 凭证才
可能产生 `validated-on-this-host`；它还必须满足：

- 当前用户所有、普通文件、单硬链接、权限 `0600`；
- 未超过 30 天，时间和正文自校验正确；
- 机器指纹、GPU/驱动、模型、GGUF、镜像和关键依赖哈希全部匹配。

复制到其他主机、配置漂移、过期或权限放宽都会使凭证失效。硬件档位匹配不能替代它。

## 发布前检查

```bash
./scripts/release-check.sh
./scripts/model-manager.py audit-sources --json
python3 ./scripts/verify-deployment.py

python3 ./scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR" --release
```

`release-check` 包含单测、Compose/候选身份检查、Git 禁入项和 Gitleaks。来源审计会联网
核对固定 revision/LFS 身份，但仍不能替代许可证和发布者信任审查。

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
明确批准的维护窗口执行；脚本无论成功、失败或中断都会恢复原生产：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/release-candidate.sh quick
```

晋级要求：

1. 一次只改变一个主要变量，并固定镜像 digest、制品 SHA256 和 Profile。
2. 按风险通过 quick、standard/full、质量、上下文、显存和恢复检查。
3. 更新 Catalog/Compose/manifest 与必要文档，再运行发布前检查。
4. 重建生产后运行 `standard`；72 小时用于灰度，168 小时用于稳定基线。

失败时恢复上一 Git revision、镜像 digest、Catalog 制品和 Profile，再运行 quick。
旧 GGUF 删除前必须完成回滚演练；任何 recreate 都会重置连续运行计时。

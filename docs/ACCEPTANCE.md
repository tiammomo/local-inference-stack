# 验收与发布

## 验收分层

| 模式 | 覆盖 | 何时运行 |
| --- | --- | --- |
| `quick` | 单测、活动权重、运行态、直连生成与思考 | 首次部署、WSL/Docker 恢复、普通本仓变更 |
| `standard` | quick + ModelPort 契约、Token、Dashboard、Tool Use、质量冒烟 | 协议、推理或 Tool 变更 |
| `full` | standard + 完整哈希、长上下文、性能、完整质量集 | 模型、量化、KV、上下文、镜像升级 |

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

验收日志和 JSON 写入 Git 忽略的 `logs/acceptance/`。只有通过的 schema v4 凭证才
可能产生 `validated-on-this-host`；它还必须满足：

- 当前用户所有、普通文件、单硬链接、权限 `0600`；
- 未超过 30 天，时间和正文自校验正确；
- 机器指纹、GPU/驱动、模型、GGUF、镜像和关键依赖哈希全部匹配；
- 验收 Profile 为 `latency`，模式对应的测试依赖没有漂移；
- 本地选择 Profile、有效 Compose 哈希和实际容器配置哈希全部匹配；
- 实际容器仍在运行且健康，镜像 ID、用户、命令、安全项、日志、端口和关键挂载没有漂移。

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
- `hostAcceptanceEvidence` 指向当前安全的 schema v4 文件。

这不保证 `readyToDeploy=true`。健康容器已占用 GPU 时，空闲显存会使
`readyToDeploy=false` 且 `nextCommands=[]`，目的是阻止第二次部署。现有服务是否已恢复
应以 `status.facts.runtimeHealthy`、`/health` 和 canonical Profile 检查为准。

若 quick 失败，失败证据只用于诊断，不会升级本机状态。不要手工编辑 JSON、
放宽权限或复制旧证据来获得 `validated-on-this-host`。

## 发布前检查

```bash
./scripts/release-check.sh
./scripts/model-manager.py audit-sources --json
python3 ./scripts/verify-deployment.py

python3 ./scripts/compatibility-check.py \
  --modelport-project "$MODELPORT_PROJECT_DIR" --release
```

`release-check` 包含全部 JSON/Python/Shell/前端语法、单测、Markdown 链接、三个 Compose
Profile、部署 manifest 文件哈希、Git 禁入项和 Gitleaks。来源审计会联网
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

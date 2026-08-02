# 贡献指南

本仓库的首要目标是让 fresh clone 的 Agent 能在不猜测主机路径、模型 URL 或运行状态的前提下安全
操作。提交应保持只读规划、显式批准、不可变制品和 loopback 边界。

## 开发环境

standalone 控制面只使用 Python 标准库，最低支持 Python 3.11；本仓库验证和用户服务基线由 uv 管理：

```bash
uv python install "$(cat .python-version)"
"$(uv python find "$(cat .python-version)")" \
  -m unittest discover -s tests -p 'test_*.py' -v
```

Dashboard 静态检查以及 ModelPort `standard/full` 固定使用 `.nvmrc` 的 Linux Node.js 24：

```bash
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm install
nvm use
node --version
```

不要替换系统 Python，也不要为 standalone 开发安装 Rust、Go、Java 或 Zig。完整配置来源见
[环境文档](docs/ENVIRONMENT.md)。

## 修改规则

- 先阅读 `AGENTS.md` 和受影响文档，运行 `./stack plan --json`；没有批准不改变 runtime 或下载制品。
- 公共用户路径优先扩展 `./stack`；`scripts/` 只保留高级诊断和兼容入口。
- Catalog 不接受猜测的 URL、文件名、阈值、revision、许可证或哈希。
- ModelPort 契约必须在两个仓库协调，应用仍负责实际工具执行、审批、沙箱和幂等。
- 不提交模型、缓存、日志、备份、本机 Profile、凭据、绝对 home 路径或原始 Prompt/回复。
- 保留 dirty worktree 中不属于当前任务的修改；提交按一个可审阅主题组织。

## 文档与生成文件

README 负责最短路径，`docs/README.md` 负责角色导航，操作细节放在对应专题，完整 CLI 契约由代码生成。
修改 CLI 后运行：

```bash
./stack reference --write --yes
python3 scripts/check-doc-links.py
python3 scripts/check-doc-commands.py
```

文档 shell 示例必须明确只读/写入边界，不应包含用户绝对路径、真实 token 或暗示绕过
`readyToDeploy` 的命令。

## 验证矩阵

```bash
./scripts/release-check.sh
./scripts/model-manager.py plan --vram-gib 8 --ram-gib 32 --json
./scripts/model-manager.py plan --vram-gib 16 --ram-gib 64 --json
./scripts/model-manager.py plan --vram-gib 24 --ram-gib 64 --json
docker compose --env-file profiles/candidate.env config >/dev/null
./scripts/acceptance-suite.sh quick
```

运行时、Catalog 或文档变化至少完成上面这些检查。reasoning、Token、Tool Use 或 ModelPort 契约变化
还必须在 compatible checkout、Node 24、有效 operations 快照和 loopback Dashboard 都就绪后运行：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh standard
```

模型、量化、KV、上下文、镜像或性能基线变化运行 `full`。更新 deployment manifest 前先完成对应
实机验收，再更新经过评审的 SHA256，最后运行 `python3 scripts/verify-manifest.py --json`。

## 提交前

检查 `git diff --check`、`git status --short` 和完整 diff；确认没有生成的本机数据、凭据、模型或
无关修改。提交信息应说明行为变化，正文记录重要安全/兼容取舍。安全问题不要公开提交，使用
[SECURITY.md](SECURITY.md) 的私密报告流程。

# Local Inference Stack 学习与实践指南

这是一份面向使用者、运维人员和后续维护者的完整学习文档。读完后，你应该能够：

- 理解本项目为什么把“选模型、下载、运行、验收、发布、运维”拆成不同阶段；
- 在 Linux/WSL NVIDIA 单卡主机上安全地完成只读评估；
- 正确理解 VRAM、上下文、KV Cache、Slot、量化和运行 Profile；
- 只在准入条件满足且得到明确批准后部署；
- 判断一份“已验证”证据是否真的适用于当前主机和当前运行配置；
- 使用直接 llama.cpp API，或通过 ModelPort 接入应用；
- 完成日常检查、故障恢复、候选发布和仓库变更验证。

> 本文中的下载、选择、启动、停止和发布命令都会改变主机状态。第一次接触项目时只运行只读的 `plan --json`。当 `readyToDeploy=false` 或 `nextCommands` 为空时，必须停止新部署，不能绕过门禁；已有健康 runtime 是否正常应另行通过 `stack status` 判断。

## 1. 项目解决什么问题

本项目不是一个简单的 `docker compose up` 封装。它负责本地推理生命周期中容易出错、又需要可追溯的部分：

1. 根据真实硬件选择经过审查的 Catalog 条目；
2. 固定模型来源 revision、GGUF 文件、字节数和 SHA256；
3. 生成当前主机专用、Git 忽略的部署配置；
4. 以固定 llama.cpp CUDA 镜像和受限容器配置运行；
5. 验证权重、API、推理、长上下文、性能和 ModelPort 契约；
6. 记录与主机、驱动、制品和有效配置绑定的验收证据；
7. 提供串行候选、自动恢复、聚合监控、备份和稳定性门禁。

它有意不负责：

- 真实业务工具的执行、审批和沙箱；
- 多租户、公网暴露和高可用；
- ModelPort 的认证、路由、计费和协议实现；
- CPU、AMD、Apple Silicon 或未经评审的多 GPU 自动部署。

## 2. 一张图理解整体架构

```text
应用 / Agent
   │
   ├─ 可选：ModelPort（认证、路由、Anthropic/OpenAI 边界、Token 准入）
   │        │
   │        └─ Docker 内网：http://qwen-runtime:8080/v1
   │
   └─ 首次独立使用：OpenAI-compatible 诊断 API
             http://127.0.0.1:18080/v1
                         │
                  llama.cpp CUDA
                         │
              本地、固定 SHA256 的 GGUF

本机运维：
  Dashboard 127.0.0.1:33004
  ModelPort 127.0.0.1:38082
  systemd user services / 聚合报告 / 备份 / 稳定性证据
```

所有宿主机端口都只绑定 loopback。本项目中的 Python 本地 HTTP 客户端会忽略代理、拒绝重定向，并拒绝非 loopback URL，避免环境代理或重定向把本地凭据带出主机。

## 3. 先掌握五个核心概念

### 3.1 Catalog 不是下载列表

[`catalog/models.json`](../catalog/models.json) 是模型选择的权威数据源。每个条目包含：

- 模型与 GGUF 发布仓库；
- 不可变 commit revision；
- 精确文件名、字节数和 SHA256；
- 许可证元数据；
- 最低与建议 VRAM、最低 RAM 和磁盘；
- 上下文、输出、Batch 和缓存参数；
- `estimated` 或 `validated` 证据状态。

Catalog 中有条目，不代表它已经在你的主机上通过验收。哈希只证明下载到的制品身份一致，也不等于发布者可信或许可证适用。

### 3.2 VRAM 不是只有模型权重

GPU 显存大致由以下部分共同消耗：

```text
权重 + KV Cache + 运行时工作区 + CUDA/驱动开销 + 并发余量
```

因此，“GGUF 文件能放进显存”不等于“128K 上下文可以稳定运行”。项目按最大单卡显存判断，不自动合并多张卡的 VRAM。

### 3.3 Context、输出预算和 KV Cache

总上下文需要容纳输入、思考和最终输出。当前验证档位的关键值是：

- 总上下文：131,072 tokens；
- 推荐推理输入：约 92K tokens；
- 最大输出：32,768 tokens；
- KV 类型：K/V 均为 Q8_0。

应用不应把总上下文全部塞给输入。必须为思考和最终回答保留空间。

### 3.4 Slot 与 Profile

项目提供两个显式 Profile：

| Profile | Slot | 每个 Slot 上下文 | 适用场景 |
| --- | ---: | ---: | --- |
| `latency` | 1 | 128K | 默认、长上下文、单任务 |
| `throughput` | 2 | 64K | 两个较短任务并发 |

切换 Profile 会重新创建运行容器。不要把两个 Slot 误解为每个都拥有完整的 128K 上下文。

### 3.5 三种“验证”不能混为一谈

| 术语 | 含义 |
| --- | --- |
| `estimated` | Catalog 中的保守估算，必须在目标主机重新验收 |
| `validated-hardware-profile-match` | 当前硬件类似已有验证档案，但还不是本机验收 |
| `validated-on-this-host` | 新鲜的 schema v4 证据与当前主机、制品、镜像、有效 Compose 和实际容器配置全部匹配 |

schema v4 证据还绑定验收模式和 `latency` Profile。旧证据、配置漂移、容器重建后的身份变化、驱动变化、权限放宽或超过有效期，都会使它失效。

## 4. 仓库地图

| 路径 | 用途 |
| --- | --- |
| `catalog/models.json` | 模型、制品、容量和许可证的权威 Catalog |
| `compose.yaml` | llama.cpp 容器、安全选项、端口、挂载和启动参数 |
| `profiles/*.env` | 版本化运行 Profile |
| `profiles/deployment.local.env` | 本机选择结果，`0600`，Git 忽略 |
| `contracts/` | 与 ModelPort 协同维护的跨仓库契约 |
| `scripts/model-manager.py` | 规划、准入、下载、选择、校验、来源审计 |
| `scripts/runtime.sh` | 受锁保护的启动、停止、切换、状态和配置断言 |
| `scripts/acceptance-suite.sh` | quick、standard、full 分层验收 |
| `scripts/release-candidate.sh` | 单卡串行候选和生产自动恢复 |
| `scripts/operations-*.py` | 聚合报告与只读 Dashboard |
| `deploy/systemd/` | 可迁移的 systemd user unit 模板 |
| `deployments/` | 已验证部署清单，不包含本机秘密或权重 |
| `quality/` | 质量和 Tool Use 合成测试用例 |
| `logs/`、`cache/`、`models/`、`backups/` | 本机数据，全部 Git 忽略 |

## 5. 第一次使用：只读评估

### 5.1 支持边界

自动化路径要求：

- Linux 或 WSL x86_64；
- 单张 NVIDIA GPU；
- NVIDIA 驱动和 Container Toolkit；
- Docker Engine 和 Docker Compose v2；
- uv 与 Python 3.11+；长期用户服务使用 `.python-version` 固定的 uv-managed Python；
- `curl`；
- util-linux 提供的 `flock`；
- 满足 Catalog 要求的当前可用 RAM、空闲显存和磁盘。

仓库不会安装这些宿主机组件。

### 5.2 唯一默认命令

```bash
./stack plan --json
```

它不会下载、选择、启动、停止或安装服务。重点阅读：

- `recommendation.id` 和 `displayName`；
- `evidenceStatus`、`catalogEvidenceStatus`、`hostAcceptanceStatus`；
- `artifactPolicy`、许可证和 `licenseReviewRequired`；
- `artifacts[].bytes`、`sha256`、模型与制品 revision；
- `runtime.contextTokens`、推荐输入和最大输出；
- 总 VRAM 与当前空闲 VRAM；
- 总 RAM 与当前可用 RAM；
- Docker、Compose 配置兼容性、Python、`curl`、`flock`；
- `caveats`；
- `readyToDeploy` 和 `nextCommands`。

容量覆盖参数只用于模拟推荐边界：

```bash
./stack plan --vram-gib 16 --ram-gib 64 --json
```

模拟结果始终不会授权部署，也不会返回可执行的部署命令。

### 5.3 单独执行准入

部署脚本在改变状态前会再次执行准入，也可以手动只读检查：

```bash
./scripts/model-manager.py admit --model qwen35-9b-q5km --json
echo "$?"  # 0=允许；3=门禁阻止
```

常见阻止原因包括：

- 运行中的服务占用了 GPU，空闲 VRAM 不足；
- 当前可用 RAM 不足；
- 多 GPU、非 NVIDIA 或非 x86_64；
- Docker/Compose/NVIDIA runtime 不可用；
- Compose 无法渲染项目所需配置；
- Python、`curl` 或 `flock` 不可用。

不要通过手写 Compose 命令、修改阈值或伪造覆盖参数绕过这些结果。

## 6. 获得批准后的部署流程

仅当计划返回 `readyToDeploy=true`、`nextCommands` 非空，而且使用者明确批准状态变更后执行：

```bash
MODEL_ID=qwen35-9b-q5km  # 必须替换为 plan 实际返回的 Catalog ID
./stack deploy --model "$MODEL_ID" --yes
./stack status
```

安全属性：

- 下载只允许 Catalog 中经过审查的 HTTPS URL；
- 跳转也只能保持 HTTPS；
- `.part` 支持断点续传；
- 下载过程由模型级文件锁串行化；
- 精确字节数和 SHA256 通过后才原子发布；
- 模型与本地配置保持当前用户所有、单硬链接和限制权限；
- 选择动作只接受已经完整校验的必需制品；
- 运行时改变由 `flock` 串行化；
- Compose 调用会清除可能覆盖项目变量的环境变量；
- 启动返回前必须达到健康状态。

下载失败时保留 `.part` 是设计行为，便于下一次安全续传。

## 7. 运行和验证

### 7.1 日常命令

```bash
./stack status --json
./stack doctor --json
./scripts/runtime.sh logs
./scripts/runtime.sh assert-profile latency
```

以下命令会改变 runtime，只在明确批准后使用：

```bash
./scripts/runtime.sh restart
./stack profile throughput --yes
./stack profile latency --yes
./scripts/runtime.sh stop
```

`assert-profile` 不只检查 Slot 数，还会把实际容器的镜像、用户、命令、环境、安全选项、日志、端口和关键挂载与有效 Compose 配置比较。

### 7.2 直接 API

```bash
curl --noproxy '*' http://127.0.0.1:18080/v1/models

curl --noproxy '*' http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-9b-q5km",
    "messages": [{"role": "user", "content": "解释什么是 KV Cache"}],
    "max_tokens": 1024
  }'
```

这个端口适合首次独立使用和本机诊断。需要认证、统一路由、Anthropic Messages、计量或 Tool Use 协议适配时，应使用 ModelPort。

### 7.3 验收分层

| 模式 | 覆盖范围 | 典型时机 |
| --- | --- | --- |
| `quick` | 单测、权重、运行身份、直接生成和思考 | 首次部署、WSL/Docker 恢复、普通运行/配置变更 |
| `standard` | quick + ModelPort 契约、Token、Dashboard、Tool Use 和质量冒烟 | 协议、推理或 Tool Use 变更 |
| `full` | standard + 完整哈希、长上下文、解码/并发性能、完整质量集 | 模型、量化、KV、上下文或镜像升级 |

```bash
./scripts/acceptance-suite.sh quick

MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh standard

MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./scripts/acceptance-suite.sh full
```

通过结果写入 Git 忽略的 `logs/acceptance/`。证据不保存真实 Prompt、回答或凭据。

## 8. ModelPort 的正确边界

稳定内部端点是：

```text
http://qwen-runtime:8080/v1
```

ModelPort 与推理容器通过外部 Docker 网络通信，宿主机 `18080` 不参与容器内部路由。跨仓库契约以 [`contracts/local-qwen-provider-v1.json`](../contracts/local-qwen-provider-v1.json) 为准。

职责边界：

| 层 | 负责 |
| --- | --- |
| 本项目 | 模型、制品完整性、推理运行、容量、验收、性能和部署证据 |
| ModelPort | 认证、路由、计量、Anthropic/OpenAI 边界、Token 准入和 Tool Use 适配 |
| 应用 / Agent | 真实工具执行、审批、沙箱、业务权限和最终结果验证 |

短生命周期 Collector 从私有环境文件或 systemd credential 读取所需凭据，写出四个时间窗口的
`aggregate-only` 快照；Dashboard 只读取快照，进程环境中不再持有 ModelPort 管理员凭据。凭据的
上游权限范围仍由 ModelPort 决定；专用只读 operations scope 必须在 ModelPort 仓库联合实现，
不能在本仓库复制或伪造其权限模型。

## 9. 长期运行与运维

### 9.1 systemd user services

基础开机/登录恢复：

```bash
uv python install "$(tr -d '[:space:]' < .python-version)"
./scripts/install-user-services.py --runtime-only --enable
```

安装器同时安装 `OnFailure` 所需的告警模板。恢复服务会等待运行时真正健康后才成功。

完整运维：

```bash
cp profiles/backup.local.env.example profiles/backup.local.env
chmod 600 profiles/backup.local.env
# 在文件中设置 MODELPORT_PROJECT_DIR

./scripts/provision-operations-secrets.py \
  --source /path/to/ModelPort/.env

./scripts/install-user-services.py --operations --enable
systemctl --user --failed
```

所有私有环境文件必须是当前用户所有的普通文件、单硬链接，并且没有 group/other 权限。systemd 服务设置了 `UMask=0077`、只读系统/主目录、最小可写目录和其他沙箱限制。
安装前可以只读渲染并交给 systemd 验证：

```bash
./scripts/install-user-services.py --check
```

### 9.2 Dashboard 与聚合报告

```bash
xdg-open http://127.0.0.1:33004
./scripts/operations-report.sh --hours 24
./scripts/operations-report.sh --hours 24 --fail-on-alert
```

Dashboard 是只读且零凭据的，只接受正确的 loopback `Host` 和同源 WebSocket Origin。凭据只在
systemd 启动的短生命周期 Collector 内出现；它每五分钟原子刷新脱敏快照。聚合数据不应包含
Prompt、回答、工具参数、原始错误、身份、IP 或请求 ID。未知终止原因会折叠为 `other`，避免原始内容进入运维证据。

### 9.3 备份和稳定性

```bash
./scripts/modelport-backup.sh create
./scripts/modelport-backup.sh verify
./scripts/modelport-backup.sh drill
./scripts/soak-check.py --minimum-hours 72
./scripts/soak-check.py --minimum-hours 168 --json
```

- 备份目录要求 `0700`，归档要求 `0600`；
- 恢复演练使用隔离的临时 PostgreSQL，不写生产数据库；
- 72 小时适合灰度证据，168 小时适合单机稳定基线；
- 容器重新创建会重新计算连续运行时间；
- 单机备份和自动恢复不等于高可用；
- 异机副本必须加密。

## 10. 串行候选发布与恢复

16GB 单卡无法同时容纳生产和完整候选。候选发布会造成短暂中断，必须在经过明确批准的维护窗口执行：

```bash
MODELPORT_PROJECT_DIR=/path/to/ModelPort \
  ./stack release quick --yes
```

脚本会：

1. 获取全局运行变更锁；
2. 在停产前检查 ModelPort checkout；
3. 记录生产是否运行及原来的 `latency`/`throughput` Profile；
4. 校验模型后停止生产；
5. 再次执行主机资源准入；
6. 启动候选并等待健康；
7. 运行直接 API、ModelPort、Token、Tool Use 和质量验收；
8. 无论成功、失败或中断，都停止候选并恢复原生产 Profile；
9. 保存限制权限的发布日志。

正式升级建议：

1. 一次只改变一个主要变量；
2. 固定镜像 digest、Catalog 制品和 Profile；
3. 按风险运行 quick、standard/full、质量、长上下文和恢复测试；
4. 更新部署 manifest；
5. 恢复生产后重新验收；
6. 收集 72/168 小时稳定性证据；
7. 在确认回滚不再需要前保留旧 GGUF。

## 11. 仓库变更和发布门禁

任何贡献至少运行：

```bash
./scripts/release-check.sh
```

它检查：

- 工作区和暂存区空白错误；
- 所有 JSON；
- Python 语法和单元测试；
- Shell 语法、systemd unit 模板，以及可用时的 ShellCheck；
- Dashboard JavaScript 语法；
- Markdown 本地链接；
- latency、throughput 和 candidate 的 Compose 身份；
- 只读主机规划；
- Git 禁入文件、被忽略却被跟踪的文件、常见凭据和绝对 home 路径；
- 部署 manifest 中所有关键仓库文件的 SHA256；
- Gitleaks（本机可选，CI 强制）。

涉及运行时、Catalog 或文档的变更还应按风险运行：

```bash
./scripts/release-check.sh --with-runtime
./scripts/model-manager.py audit-sources --json
python3 scripts/verify-deployment.py
```

`audit-sources` 会联网核对固定 revision 和 Hugging Face LFS 身份；它仍不替代许可证和发布者信任审查。

变更与验收矩阵：

| 变更 | 最低验证 |
| --- | --- |
| 文档、非运行脚本 | `release-check.sh` |
| Catalog、Compose、运行脚本 | release check + representative VRAM 边界 + quick |
| 推理、Token、Tool Use、ModelPort 契约 | standard |
| 模型、量化、KV、上下文、镜像 | full + 串行候选 + 稳定性证据 |
| 生产基线或门限 | 更新文档和 deployment manifest |

## 12. 故障排查

### 12.1 `readyToDeploy=false`

先看 `caveats`，不要直接启动：

```bash
./stack plan --json
nvidia-smi
docker info
docker compose version
```

如果 `./stack status --json` 显示已知 runtime 健康，则低空闲显存只是阻止再次部署；
不要停止或重建健康容器。如果是未知进程、共享 GPU、多 GPU 或平台不受支持，停止
自动部署并设计独立 Profile。

### 12.2 运行时不健康

```bash
./scripts/runtime.sh status
./scripts/runtime.sh logs
./scripts/model-manager.py verify --cached
./scripts/runtime.sh assert-profile latency
```

常见原因：

- 权重不完整或被修改；
- GPU 显存不足；
- Profile 与 Slot 预期不一致；
- 容器实际配置与有效 Compose 漂移；
- ModelPort 外部网络不存在；
- 运行用户无法写 cache。

确认原因后再执行受控 `restart`。不要使用裸 Compose 命令绕过锁和配置清理。

### 12.3 ModelPort 找不到推理服务

检查：

```bash
docker network inspect modelport_default
docker inspect qwen35-9b-q5km
curl --noproxy '*' http://127.0.0.1:18080/health
curl --noproxy '*' http://127.0.0.1:38082/livez
```

确认网络别名是 `qwen-runtime`，ModelPort 使用的是容器内端点而不是宿主机端口。

### 12.4 只有思考，没有正文

检查精确 Token 计数、输入长度、思考预算和 `max_tokens`。降低输入或思考预算，为最终回答预留输出，不要依赖静默截断。

### 12.5 运维报告失败

检查：

```bash
stat -c '%a %U %n' profiles/operations.secrets.env profiles/backup.local.env
systemctl --user --failed
journalctl --user -u qwen-model-operations-report.service -n 100
curl --noproxy '*' http://127.0.0.1:33004/api/health
```

私有配置权限不是 `0600`、文件是符号链接、ModelPort 凭据失效、备份过期或磁盘不足都会触发失败或告警。

## 13. 推荐学习实验

这些实验按风险递增排列。

### 实验 A：完全只读地理解计划

1. 运行 `plan --json`；
2. 找出推荐模型、下载大小和 SHA256；
3. 解释 `fits`、`resourceAvailableNow` 和 `readyToDeploy` 的区别；
4. 找出阻止部署的每条 caveat；
5. 不执行 `nextCommands`。

### 实验 B：阅读配置，不启动

```bash
python3 -c 'from scripts.runtime_identity import rendered_compose; import json; print(json.dumps(rendered_compose("latency"), indent=2))'
./scripts/candidate-runtime.sh config
```

比较容器名、端口、Slot、cache 路径和重启策略。

### 实验 C：在已有健康部署上验证身份

```bash
./scripts/runtime.sh status
./scripts/runtime.sh assert-profile latency
./scripts/acceptance-suite.sh quick
```

观察 schema v4 证据如何绑定当前配置。这个实验会发送合成推理请求，不应在未知的共享生产 GPU 上直接运行。

### 实验 D：维护者静态检查

```bash
./scripts/release-check.sh
python3 scripts/verify-manifest.py --json
```

修改一个被 manifest 跟踪的文件，观察门禁失败；恢复或更新经过评审的 manifest 后再验证。

## 14. 术语表

| 术语 | 解释 |
| --- | --- |
| GGUF | llama.cpp 常用模型制品格式 |
| 量化 | 用较低精度存储权重或 KV，以换取容量和速度 |
| KV Cache | Attention 为历史 Token 保存的 Key/Value 状态 |
| Slot | llama.cpp 可独立处理一个请求序列的上下文槽 |
| TTFT | Time to First Token，首 Token 延迟 |
| P95 | 95% 样本不超过的延迟值 |
| digest/SHA256 | 不可变身份校验值，不代表可信度 |
| fail-closed | 无法确认安全或兼容时默认拒绝 |
| host acceptance | 绑定当前主机和当前配置的验收 |
| synthetic traffic | 为验收生成、与真实用户无关的流量 |
| soak | 持续运行一段时间后检查稳定性证据 |

## 15. 继续阅读

- [文档导航](README.md)
- [首次部署](GETTING_STARTED.md)
- [环境与配置来源](ENVIRONMENT.md)
- [API 使用](API.md)
- [硬件与模型](HARDWARE_GUIDE.md)
- [架构与边界](ARCHITECTURE.md)
- [ModelPort 接入](MODELPORT.md)
- [验收与发布](ACCEPTANCE.md)
- [运维与恢复](OPERATIONS.md)
- [升级与回滚](UPGRADING.md)
- [控制面参考](REFERENCE.md)
- [贡献指南](../CONTRIBUTING.md)
- [当前验证档案](../deployments/qwen3.5-9b-rtx5070ti/README.md)
- [Agent 操作约束](../AGENTS.md)

## 16. 统一控制面的学习路径

`./stack` 是面向普通用户和 Agent 的稳定入口，`scripts/` 保留为高级诊断和兼容层。所有
`--json` 结果都包含 `schemaVersion`、`command`、`status`、`code`、`summary`、`facts` 和
`nextActions`；退出码固定为：0 成功、2 用法、3 准入、4 配置、5 外部依赖、6 完整性、7 恢复。

建议按以下顺序学习，前八条完全只读：

```bash
./stack plan --json                 # 硬件、推荐、来源、许可证和下一步
./stack doctor --json               # 平台、依赖、配置漂移、待恢复事务
./stack config check --json         # 类型化配置与派生 Profile/Compose/Dashboard
./stack migrate --check --json      # schema 当前版本与 N-1 兼容情况
./stack storage report --json       # models/cache/logs/backups 占用与保护引用
./stack credentials audit --json    # 只读权限元数据；永不读取凭据值
./stack bundle verify FILE --json   # 离线校验 bundle 全部成员、大小与哈希
./stack reconcile --json            # 只显示中断事务的恢复计划
```

需要写入或运行负载的命令均要求显式 `--yes`：

```bash
./stack profile throughput --yes
./stack accept quick --yes
./stack release quick --yes
./stack bundle create --model qwen35-9b-q5km --include-model \
  --output stack.tar --yes
./stack bundle import stack.tar --yes
./stack calibrate run --yes
```

离线 bundle 导入只会原子写入与本地 Catalog 身份完全一致的制品，不会选择模型或启动服务；
导入后必须重新运行当前主机准入。`calibrate` 只生成候选对比报告，不会在线调整生产参数。
`storage gc` 默认 dry-run，且只处理过期的 `.part`/`.tmp`。

可复用验证与本机 acceptance 是两层证据。创建 attestation 时会绑定 acceptance、Git revision、
配置身份和平台；dirty tree 只能得到草稿。正式证明必须使用 Minisign 或 Cosign 的分离签名并
执行外部密码学验证，自哈希 JSON 不能提升 Catalog：

```bash
./stack attest create --evidence logs/acceptance/FILE.json --output validation.json
./stack attest sign validation.json --tool minisign --secret-key KEY \
  --signature validation.minisig --yes
./stack attest verify validation.json --require-signature --tool minisign \
  --public-key KEY.pub --signature validation.minisig
```

发生中断时不要先运行新的变更命令。持久事务位于 Git 忽略的
`cache/control-plane/transaction.json`，记录原 Profile、目标、状态、历史和下一恢复动作；
新部署、Profile 切换和候选发布都会先拒绝未完成事务。查看并批准 `reconcile` 后，控制面调用
加固的恢复脚本，健康与身份确认成功后才将事务标记为完成。

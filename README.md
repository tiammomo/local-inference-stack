# Local Inference Stack

面向 Linux/WSL NVIDIA 单机的本地 LLM 推理栈：硬件评估、Catalog 选型、可校验
GGUF 下载、llama.cpp CUDA 运行、验收、发布和运维。模型权重不进入 Git。

当前可执行 Catalog 只保留 RTX 5070 Ti 16GB、Qwen3.5-9B Q5_K_M、128K 单 Slot、
Q8_0 KV 这一条历史实机档案。它仍为 `provisional`，等待当前 qualification；没有实机
证据的估算模型已移出可执行 Catalog。现阶段 planner 不会生成下载、选择或启动命令，已有
健康实例不受此冻结影响。

## 快速开始

只读诊断可运行于 Linux/WSL x86_64 NVIDIA 主机；自动新部署当前只接受 Tier-1 WSL2 /
RTX 5070 Ti 精确档案。其余要求：NVIDIA GPU/驱动、NVIDIA Container Toolkit、
Docker Compose v2、uv、Python 3.11+、`curl` 和 util-linux `flock`。仓库的已验证开发与
用户服务基线由 `.python-version` 固定为 uv-managed CPython 3.14.6；最低兼容门槛仍是 3.11。
ModelPort `standard/full` 是可选联合验收，并固定使用 `.nvmrc` 声明的 Linux Node.js 24。

```bash
git clone git@github.com:tiammomo/local-inference-stack.git
cd local-inference-stack

# 唯一默认动作：只读，不下载、不启动、不改配置
./stack plan --json
```

先检查推荐模型、`evidenceStatus`、`hostAcceptanceStatus`、下载大小、固定 revision、
许可证、SHA256、空闲显存和 `caveats`。然后区分两种情况。

当前 Catalog 处于证据收敛期，即使硬件准入通过，`catalogDeploymentEligible=false` 仍会使
`readyToDeploy=false`、`actionPlan=null`。这是硬停止条件；不要直接调用旧脚本绕过。

### 新主机首次部署

只有 `readyToDeploy=true` 且存在完整的类型化 `actionPlan`，并得到用户明确批准后，
才执行：

```bash
MODEL_ID=qwen35-9b-q5km # 替换为 plan 返回的 id
./stack deploy --model "$MODEL_ID" --yes
```

`./stack` 是稳定公共入口；`scripts/` 下的命令只在本文或运维 Runbook 明确列出时作为
高级诊断/兼容入口。下载支持断点续传，
只有精确字节数和 SHA256 匹配才会发布。部署会重新执行主机准入，并由文件锁和持久事务双重
串行化；`quick` 验证权重、实际容器配置、生成和思考，并生成与本机及当前有效配置绑定的限时
schema v4 验收凭证。

### 已部署主机恢复运行

不要因为模型已占用显存、`readyToDeploy=false` 就重复部署。先检查现有实例：

```bash
./stack doctor --json
./stack status --json
curl --noproxy '*' http://127.0.0.1:18080/health
```

若 `runtimeHealthy=true`、健康接口返回 `{"status":"ok"}`，项目已经在运行。此时 plan 中
的低空闲显存是容量占用证据，只阻止新的部署动作。需要单独重新确认当前主机 quick
证据时，才运行：

```bash
./scripts/acceptance-suite.sh quick
```

### 维护窗口升级与回滚

公共入口默认只读；确认准入对象、scope 与回滚计划并取得批准后才添加 `--yes`：

```bash
./stack upgrade --model CATALOG_ID
./stack rollback
```

typed rollback 仅支持 `same-controller-same-catalog-anchor-v1`：同一主机、同一精确 controller、
当前 Catalog 仍认可的 `latency` 锚点，以及本地 artifact/image。它不联网、不 pull、不 checkout
Git；事务内 `quick --no-record` 只作服务门禁，不构成 qualification。当前 Catalog 只有一个
provisional 条目，不能形成合法 validated rollback/LTS 模型对，因此真实 upgrade/rollback 按设计
fail closed。操作和恢复细节见[升级与回滚](docs/UPGRADING.md)。

完整的新机、已部署恢复和 WSL 自启流程见[首次部署](docs/GETTING_STARTED.md)与
[运维与恢复](docs/OPERATIONS.md)。

## 使用与运维

直连诊断 API 只监听 `127.0.0.1:18080`：

```bash
curl --noproxy '*' http://127.0.0.1:18080/v1/models
curl --noproxy '*' http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-9b-q5km","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'
```

请求字段、流式响应、reasoning、Token/上下文边界和错误排查见 [API 使用](docs/API.md)。

常用命令：

```bash
./stack status
./stack status --scope integrated
./stack doctor
./stack verify --scope config
./stack verify --scope standalone
./stack storage report
./stack credentials audit
./scripts/runtime.sh logs
./scripts/install-user-services.py --runtime-only --enable
```

应用长期接入可选 ModelPort Anthropic Messages 网关；业务工具的执行、审批、沙箱和
幂等不属于本仓库。

## 文档

- [按角色浏览全部文档](docs/README.md)
- [完整学习与实践指南](docs/LEARNING_GUIDE.md)
- [环境、配置来源与安装边界](docs/ENVIRONMENT.md)
- [API 使用](docs/API.md)
- [升级与回滚](docs/UPGRADING.md)
- [项目优化路线图](docs/ROADMAP.md)
- [架构决策：可信单机 Appliance](docs/decisions/0001-trusted-single-host-appliance.md)
- [架构决策：长期收口与兼容终止](docs/decisions/0002-long-term-appliance-simplification.md)
- [首次部署](docs/GETTING_STARTED.md)
- [硬件与模型](docs/HARDWARE_GUIDE.md)
- [架构与边界](docs/ARCHITECTURE.md)
- [ModelPort 接入](docs/MODELPORT.md)
- [验收](docs/ACCEPTANCE.md)
- [运维与恢复](docs/OPERATIONS.md)
- [控制面命令、schema 与退出码参考](docs/REFERENCE.md)
- [当前验证档案](deployments/qwen3.5-9b-rtx5070ti/README.md)
- [贡献指南](CONTRIBUTING.md)

自动化操作约束见 [AGENTS.md](AGENTS.md)。

## 安全边界

- 仅支持可信单用户主机；不要把 `18080`、ModelPort 或运行台直接暴露到网络。
- 多 GPU、CPU、Apple Silicon、AMD、共享生产 GPU 均需人工设计独立 Profile。
- 固定哈希只证明制品身份，不替代模型、GGUF 发布者和许可证审查。
- 模型、缓存、日志、备份、本地 Profile 和凭证均应保持 Git 忽略。

安全问题使用 GitHub 私密漏洞报告，详见 [SECURITY.md](SECURITY.md)。

## License

仓库自产内容采用 [MIT License](LICENSE)；模型、GGUF、镜像和其他第三方组件遵循
各自许可证。

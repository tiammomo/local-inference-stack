# 文档导航

先从与当前角色最相关的一条路径开始。所有运行状态变更仍受仓库根目录
[`AGENTS.md`](../AGENTS.md) 约束；不确定时先运行只读 `./stack plan --json`。

## 第一次使用

1. [环境与配置来源](ENVIRONMENT.md)：确认 uv/Python、Docker、NVIDIA 和可选 Node 的来源。
2. [首次部署](GETTING_STARTED.md)：完成只读规划、人工批准、Catalog 部署和 quick 验收。
3. [API 使用](API.md)：从 loopback 直连 API 发起健康、生成、流式和 reasoning 请求。

## 日常运维

1. [运维与恢复](OPERATIONS.md)：健康检查、WSL/Docker 恢复、systemd、Dashboard 和备份。
2. [验收与发布](ACCEPTANCE.md)：选择 quick、standard 或 full，并解释本机证据。
3. [升级与回滚](UPGRADING.md)：评审 Git、schema、Catalog、镜像和 Profile 变化。
4. [当前验证档案](../deployments/qwen3.5-9b-rtx5070ti/README.md)：查看已验证硬件与不可变身份。

## 应用与网关集成

- [API 使用](API.md)：独立 llama.cpp OpenAI-compatible 诊断入口。
- [ModelPort 接入](MODELPORT.md)：Anthropic Messages、逻辑模型、Token 准入和 Tool Use 边界。
- [架构与边界](ARCHITECTURE.md)：仓库、ModelPort 与应用各自负责什么。

## 维护与开发

- [贡献指南](../CONTRIBUTING.md)：开发环境、测试矩阵、文档生成、manifest 和安全要求。
- [控制面参考](REFERENCE.md)：由代码生成的完整 CLI 语法、状态影响、批准要求和退出码。
- [项目路线图](ROADMAP.md)：各 Phase 当前状态、依赖和剩余工作。
- [学习与实践指南](LEARNING_GUIDE.md)：从模型制品到验收、运维和控制面的完整背景。
- [ADR-0001](decisions/0001-trusted-single-host-appliance.md)：可信单机 appliance 的设计决策。

`README.md` 提供最短路径，本页提供角色导航，`REFERENCE.md` 只记录机器契约；高级诊断脚本
保留在 `scripts/`，但新的稳定用户流程应优先使用 `./stack`。

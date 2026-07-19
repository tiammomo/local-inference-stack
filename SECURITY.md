# Security policy / 安全策略

安全修复只针对 `main` 最新版本。模型权重、第三方 GGUF、GPU 驱动、Docker、
NVIDIA Container Toolkit 和 ModelPort 有各自独立的供应链与支持边界。

请通过 GitHub 仓库的 **Private vulnerability reporting / Security advisory** 私下报告
漏洞，不要在公开 Issue 中粘贴 Token、`.env`、Prompt、模型回复、工具参数、日志或
主机身份信息。报告应尽量包含受影响提交、无敏感数据的复现步骤和预期安全边界。

Only the latest `main` revision is supported. Report vulnerabilities through
GitHub private vulnerability reporting or a private security advisory. Never
include credentials, prompts, responses, tool arguments, raw logs, or host
identity data in a public issue.

The default supported boundary is a trusted single-user Linux/WSL NVIDIA host
with loopback-only services. LAN/public exposure, multi-tenant isolation,
business tool execution, and arbitrary third-party ModelPort providers require
their own security review.

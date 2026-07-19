# 安全与 GitHub 发布检查

## 威胁边界

本仓库下载并运行大型第三方二进制模型制品与容器镜像。主要风险是供应链漂移、意外
暴露监听端口、把本地凭证/日志/权重提交到 Git，以及 Agent 未经批准触发大下载或
替换现有运行态。

已有控制：

- llama.cpp 镜像固定 digest；
- GGUF 固定 HTTPS 来源、字节数和 SHA256；
- `.part` 校验成功后原子发布；
- `plan` 默认只读，所有下载和选择都要求显式 `--yes`；
- 模型、缓存、日志、本地 Profile 和凭证由 `.gitignore` 排除；
- 服务绑定 loopback；容器非 root、只读根文件系统、`cap_drop: ALL`、
  `no-new-privileges`；
- systemd 使用当前 checkout 渲染，不在仓库记录用户名或绝对家目录；
- quick 验收无需读取 ModelPort 密钥；运营密钥只最小化复制到本地 `0600` 文件。

固定哈希只证明身份一致。发布者账户、上游仓库或固定对象本身仍可能不可信；模型许可、
数据政策和组织安全要求必须单独审查。

## 发布前命令

```bash
./scripts/release-check.sh
./scripts/release-check.sh --with-runtime
git status --short
```

再检查仓库对象而不只检查工作树：

```bash
git ls-files | rg '(^|/)(\.env|.*secret.*|.*\.gguf|.*\.part)$' || true
git grep -nE '(BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})' || true
```

如环境安装了 Gitleaks，应对完整历史运行 `gitleaks git --redact --no-banner`。任何命中
都必须逐项确认；不能只靠“当前文件已删除”。

## 发布结论用语

不要声称“绝对没有安全问题”。可以说明检查范围、工具、时间、提交和剩余风险：

- 当前 Git 工作树/历史是否发现凭证；
- 运行时监听与容器权限是否符合基线；
- 哪个模型/硬件是实机验证，哪些仅为估算；
- 第三方模型、制品和容器的供应链风险仍由使用者接受；
- ModelPort 与公网暴露不属于首次直连部署的默认安全边界。

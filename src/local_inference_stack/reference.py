"""Generated CLI/schema/exit-code reference."""

from __future__ import annotations

from .migration import CURRENT
from .result import ExitCode


COMMANDS = {
    "plan": "只读评估硬件并给出 Catalog 推荐。",
    "doctor": "检查平台、依赖、配置和待恢复事务。",
    "status": "报告 runtime 与持久事务状态。",
    "verify": "在不改变运行状态的前提下验证仓库、配置或模型。",
    "deploy": "取得 --yes 批准后解释绑定 Catalog identity 的类型化部署计划。",
    "upgrade": "在维护窗口内按类型化事务替换 production，并保存精确回滚锚点。",
    "rollback": "只读检查或显式消费唯一的本机不可变回滚锚点。",
    "accept": "取得 --yes 批准后运行指定验收层级。",
    "release": "取得 --yes 批准后执行串行候选发布与生产恢复。",
    "profile": "通过持久事务切换固定生产 Profile。",
    "reconcile": "查看或显式修复未完成事务。",
    "config": "校验或确定性渲染运行配置。",
    "attest": "创建、签名或验证可复用验收证明。",
    "bundle": "创建、验证或显式导入离线 bundle。",
    "calibrate": "规划或运行只生成报告的本机校准。",
    "storage": "报告存储或保守清理临时文件。",
    "credentials": "审计凭据元数据或显式迁移凭据。",
    "migrate": "检查 schema 兼容性，不静默迁移。",
    "reference": "从控制面定义生成本参考。",
}


# Keep leaf command syntax here so the generated reference and documentation smoke
# checker share one reviewed contract. tests/test_control_plane.py verifies that the
# paths remain in sync with the argparse tree.
COMMAND_DETAILS = (
    {
        "path": "plan",
        "syntax": "./stack plan [--model CATALOG_ID] [--vram-gib GIB] [--ram-gib GIB] [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "探测或模拟主机容量，并返回 Catalog 推荐、证据状态和下一步。容量覆盖只用于模拟。",
        "example": "./stack plan --json",
    },
    {
        "path": "doctor",
        "syntax": "./stack doctor [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "检查平台、依赖、配置漂移和未完成事务；不启动服务。",
        "example": "./stack doctor --json",
    },
    {
        "path": "status",
        "syntax": "./stack status [--scope {standalone,integrated,all}] [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "按 scope 报告 runtime、持久事务和可选联合运维组件；默认只检查 standalone。",
        "example": "./stack status --json",
    },
    {
        "path": "verify",
        "syntax": "./stack verify [--scope {repository,config,model,standalone,integrated,all}] [--model CATALOG_ID] [--cached] [--json]",
        "state": "只读；可能执行本地测试或模型哈希读取",
        "approval": "不需要",
        "description": "验证仓库、类型化配置、Catalog 制品或指定范围的部署组件。",
        "example": "./stack verify --scope config --json",
    },
    {
        "path": "deploy",
        "syntax": "./stack deploy [--model CATALOG_ID] --yes [--json]",
        "state": "写入并启动 runtime；可能下载制品",
        "approval": "必须 --yes，且 plan 必须准入",
        "description": "只解释当前只读 plan 返回且与 Catalog identity 绑定的类型化部署步骤。",
        "example": "./stack deploy --model qwen35-9b-q5km --yes",
    },
    {
        "path": "upgrade",
        "syntax": "./stack upgrade --model CATALOG_ID [--qualification {quick,full}] [--yes] [--json]",
        "state": "默认只读；--yes 在单 runtime 维护窗口中下载、停止、替换并执行所选门禁",
        "approval": "执行时必须 --yes，source/target/host/attestation 均须准入",
        "description": "默认 quick 仅作服务门禁；显式 full 将事务绑定 qualification 放在 rollback pointer 发布前，仍不会自动晋级 Catalog。",
        "example": "MODELPORT_PROJECT_DIR=/absolute/path/to/ModelPort ./stack upgrade --model CATALOG_ID --qualification full --json",
    },
    {
        "path": "rollback",
        "syntax": "./stack rollback [--yes] [--json]",
        "state": "默认只读；--yes 停止当前 runtime 并恢复唯一 active anchor",
        "approval": "执行时必须 --yes；不接受 model、路径、URL 或 image 参数",
        "description": "离线消费私有内容寻址锚点，精确复核 Catalog、制品、镜像、主机和 controller 后恢复并 quick smoke。",
        "example": "./stack rollback --json",
    },
    {
        "path": "accept",
        "syntax": "./stack accept [{quick,standard,full}] --yes [--json]",
        "state": "运行测试负载并写验收证据",
        "approval": "必须 --yes",
        "description": "运行指定验收层级；standard/full 还需要显式 ModelPort 环境。",
        "example": "./stack accept quick --yes",
    },
    {
        "path": "release",
        "syntax": "./stack release [{quick,long}] --yes [--json]",
        "state": "停止生产、运行候选并恢复生产",
        "approval": "必须 --yes",
        "description": "在单 GPU 上串行执行候选验收，始终校验生产恢复身份。",
        "example": "MODELPORT_PROJECT_DIR=/path/to/ModelPort ./stack release quick --yes",
    },
    {
        "path": "profile",
        "syntax": "./stack profile {latency,throughput} --yes [--json]",
        "state": "重建 runtime",
        "approval": "必须 --yes",
        "description": "通过持久事务切换固定生产 Profile。",
        "example": "./stack profile throughput --yes",
    },
    {
        "path": "reconcile",
        "syntax": "./stack reconcile [--yes] [--json]",
        "state": "默认只读；--yes 执行恢复",
        "approval": "修复时必须 --yes",
        "description": "查看或修复中断的持久运行事务。",
        "example": "./stack reconcile --json",
    },
    {
        "path": "config check",
        "syntax": "./stack config check [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "校验类型化配置及其派生 Profile、Compose 和 Dashboard 文件。",
        "example": "./stack config check --json",
    },
    {
        "path": "config render",
        "syntax": "./stack config render [--write --yes] [--json]",
        "state": "默认只渲染；--write 写派生文件",
        "approval": "写入时必须 --yes",
        "description": "确定性渲染类型化配置；无 --write 时只返回内容。",
        "example": "./stack config render --json",
    },
    {
        "path": "attest create",
        "syntax": "./stack attest create --evidence PATH --output PATH [--json]",
        "state": "写显式 --output 文件",
        "approval": "输出路径即显式目标",
        "description": "从本机验收证据创建不含凭据的可复用 attestation 草稿。",
        "example": "./stack attest create --evidence logs/acceptance/FILE.json --output attestation.json",
    },
    {
        "path": "attest sign",
        "syntax": "./stack attest sign PATH --tool {minisign,cosign} --secret-key PATH --signature PATH --yes [--json]",
        "state": "调用签名工具并写签名文件",
        "approval": "必须 --yes",
        "description": "对已审阅 attestation 生成外部签名；私钥内容不进入结果。",
        "example": "./stack attest sign attestation.json --tool minisign --secret-key KEY --signature attestation.minisig --yes",
    },
    {
        "path": "attest verify",
        "syntax": "./stack attest verify PATH [--require-signature] [--for-promotion --trusted-key-sha256 SHA256] [--tool {minisign,cosign} --public-key PATH --signature PATH] [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "校验结构、自哈希和生命周期；分离签名必须显式选择密码学验证，Catalog 晋级还必须用外部固定公钥指纹重检当前输入。",
        "example": "./stack attest verify attestation.json --for-promotion --tool minisign --public-key KEY.pub --signature attestation.minisig --trusted-key-sha256 SHA256",
    },
    {
        "path": "bundle create",
        "syntax": "./stack bundle create --model CATALOG_ID --output PATH [--include-model] [--image-archive PATH] --yes [--json]",
        "state": "写离线 bundle；可包含大制品",
        "approval": "必须 --yes",
        "description": "仅为 LTS 生命周期条目创建带成员清单、大小和哈希的离线复现 bundle。",
        "example": "./stack bundle create --model qwen35-9b-q5km --output stack.tar --yes",
    },
    {
        "path": "bundle verify",
        "syntax": "./stack bundle verify PATH [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "在解包或写入前验证 bundle 路径、成员、大小和哈希。",
        "example": "./stack bundle verify stack.tar --json",
    },
    {
        "path": "bundle import",
        "syntax": "./stack bundle import PATH --yes [--json]",
        "state": "导入匹配 Catalog 的制品；不选择或启动",
        "approval": "必须 --yes",
        "description": "原子导入已验证制品，之后仍需重新运行主机准入。",
        "example": "./stack bundle import stack.tar --yes",
    },
    {
        "path": "calibrate plan",
        "syntax": "./stack calibrate plan [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "显示离线候选 Profile 校准计划，不改生产。",
        "example": "./stack calibrate plan --json",
    },
    {
        "path": "calibrate run",
        "syntax": "./stack calibrate run [--output PATH] --yes [--json]",
        "state": "运行本地探针并写候选报告；不改生产",
        "approval": "必须 --yes",
        "description": "执行报告型校准，结果需人工评审后才能进入 Profile。",
        "example": "./stack calibrate run --output calibration.json --yes",
    },
    {
        "path": "storage report",
        "syntax": "./stack storage report [--json]",
        "state": "只读",
        "approval": "不需要",
        "description": "报告 models、cache、logs、backups 占用和保护引用。",
        "example": "./stack storage report --json",
    },
    {
        "path": "storage gc",
        "syntax": "./stack storage gc [--older-than-days DAYS] [--yes] [--json]",
        "state": "默认 dry-run；--yes 删除受限临时文件",
        "approval": "删除时必须 --yes",
        "description": "只处理超龄且未被生产、事务或回滚锚点引用的 .part/.tmp。",
        "example": "./stack storage gc --older-than-days 14 --json",
    },
    {
        "path": "credentials audit",
        "syntax": "./stack credentials audit [--json]",
        "state": "只读元数据",
        "approval": "不需要",
        "description": "只检查凭据路径、类型和权限，不读取或输出值。",
        "example": "./stack credentials audit --json",
    },
    {
        "path": "credentials migrate-systemd",
        "syntax": "./stack credentials migrate-systemd {operations,backup,alerting} --yes [--json]",
        "state": "写 systemd credential；保留兼容源",
        "approval": "必须 --yes",
        "description": "把选定本地凭据显式迁移到 systemd credential backend。",
        "example": "./stack credentials migrate-systemd operations --yes",
    },
    {
        "path": "migrate",
        "syntax": "./stack migrate [--check | --yes] [--json]",
        "state": "默认/--check 只读；--yes 仅写入已验证的兼容本机迁移",
        "approval": "写入时必须 --yes",
        "description": "按显式可读集合检查 schema；兼容的旧 selected profile 只在制品全量验证后原子归一，不静默改写。",
        "example": "./stack migrate --check --json",
    },
    {
        "path": "reference",
        "syntax": "./stack reference [--check | --write --yes] [--json]",
        "state": "默认渲染；--check 只读；--write 更新生成文档",
        "approval": "写入时必须 --yes",
        "description": "渲染或校验本页；CLI 变更后应重新生成。",
        "example": "./stack reference --check --json",
    },
)


def command_paths() -> tuple[str, ...]:
    return tuple(item["path"] for item in COMMAND_DETAILS)


def render() -> str:
    lines = [
        "# 控制面参考（生成文件）",
        "",
        "> 由 `./stack reference --write --yes` 生成；不要手工编辑。",
        "",
        "全局 `--json` 可以放在命令前后。结构化输出和退出码稳定；命令是否改变本地状态、",
        "是否需要批准，以各节为准。`scripts/` 不构成公共契约；普通生命周期不要直接调用，只有明确列出它的运维或贡献者 Runbook 例外。",
        "",
        "## 顶层命令",
        "",
    ]
    lines.extend(f"- `./stack {name}` — {description}" for name, description in COMMANDS.items())
    lines.extend(["", "## 完整语法", ""])
    for detail in COMMAND_DETAILS:
        lines.extend(
            [
                f"### `./stack {detail['path']}`",
                "",
                detail["description"],
                "",
                f"- 状态：{detail['state']}。",
                f"- 批准：{detail['approval']}。",
                f"- 语法：`{detail['syntax']}`",
                f"- 示例：`{detail['example']}`",
                "",
            ]
        )
    lines.extend(["## 稳定退出码", ""])
    descriptions = {
        ExitCode.SUCCESS: "成功",
        ExitCode.USAGE: "参数或显式批准错误",
        ExitCode.ADMISSION: "主机/资源准入拒绝",
        ExitCode.CONFIG: "配置或 schema 错误",
        ExitCode.EXTERNAL: "外部命令、服务或网络错误",
        ExitCode.INTEGRITY: "哈希、签名或 bundle 完整性错误",
        ExitCode.RECOVERY: "存在未恢复事务或恢复失败",
    }
    lines.extend(f"- `{int(code)}` — {descriptions[code]}" for code in ExitCode)
    lines.extend(["", "## 结构化结果 schema", ""])
    lines.extend(
        [
            "所有带 `--json` 的命令输出一个对象：",
            "",
            "```json",
            '{"schemaVersion":1,"command":"plan","status":"ok","code":0,"summary":"...","facts":{},"nextActions":[]}',
            "```",
            "",
            "`nextActions[].requiresApproval=true` 表示该动作会改变主机状态。",
            "",
            "## 本地 schema 版本",
            "",
        ]
    )
    schema_notes = {
        "attestation": "仅 v2 可读；v1 缺少当前信任绑定，拒绝读取",
        "bundle": "可读 v1/v2；v1 仅支持纯制品，未绑定镜像 archive 必须重建",
        "commandResult": "仅 v1 可读",
        "rollbackPointer": "仅 v1 可读；私有单调 generation 与精确 CAS",
        "rollbackSpec": "仅 v1 可读；同 controller、同 Catalog、本机离线恢复",
        "runtimeProfiles": "可读 v1/v2；v1 只读检查",
        "transaction": "可读 v1/v2；v1 必须分类并经显式 reconcile 处理",
    }
    for name, version in sorted(CURRENT.items()):
        compatibility = schema_notes[name]
        lines.append(f"- `{name}`: 当前 `{version}`（{compatibility}）")
    return "\n".join(lines) + "\n"

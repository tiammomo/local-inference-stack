"""Generated CLI/schema/exit-code reference."""

from __future__ import annotations

from .migration import CURRENT
from .result import ExitCode


COMMANDS = {
    "plan": "Read-only hardware assessment and catalog recommendation.",
    "doctor": "Check platform, prerequisites, configuration, and pending recovery.",
    "status": "Report runtime and durable transaction status.",
    "verify": "Run repository/runtime verification without changing state.",
    "deploy": "Execute only catalog-generated deployment commands after --yes.",
    "accept": "Run an acceptance tier after --yes.",
    "release": "Run the serial candidate release workflow after --yes.",
    "profile": "Switch a fixed production profile through a durable transaction.",
    "reconcile": "Inspect or explicitly repair an unfinished transaction.",
    "config": "Validate or deterministically render runtime profiles.",
    "attest": "Create and verify reusable-validation drafts.",
    "bundle": "Create, verify, or explicitly import an offline bundle.",
    "calibrate": "Plan or run report-only local calibration.",
    "storage": "Report storage or conservatively garbage-collect temporary files.",
    "credentials": "Audit credential metadata without reading values.",
    "migrate": "Check schema compatibility; never migrate silently.",
    "reference": "Generate this reference from control-plane definitions.",
}


def render() -> str:
    lines = [
        "# 控制面参考（生成文件）",
        "",
        "> 由 `./stack reference --write` 生成；不要手工编辑。",
        "",
        "## 公共命令",
        "",
    ]
    lines.extend(f"- `./stack {name}` — {description}" for name, description in COMMANDS.items())
    lines.extend(["", "## 稳定退出码", ""])
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
    lines.extend(
        f"- `{name}`: `{version}`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）"
        for name, version in sorted(CURRENT.items())
    )
    return "\n".join(lines) + "\n"

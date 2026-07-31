# 控制面参考（生成文件）

> 由 `./stack reference --write` 生成；不要手工编辑。

## 公共命令

- `./stack plan` — Read-only hardware assessment and catalog recommendation.
- `./stack doctor` — Check platform, prerequisites, configuration, and pending recovery.
- `./stack status` — Report runtime and durable transaction status.
- `./stack verify` — Run repository/runtime verification without changing state.
- `./stack deploy` — Execute only catalog-generated deployment commands after --yes.
- `./stack accept` — Run an acceptance tier after --yes.
- `./stack release` — Run the serial candidate release workflow after --yes.
- `./stack profile` — Switch a fixed production profile through a durable transaction.
- `./stack reconcile` — Inspect or explicitly repair an unfinished transaction.
- `./stack config` — Validate or deterministically render runtime profiles.
- `./stack attest` — Create and verify reusable-validation drafts.
- `./stack bundle` — Create, verify, or explicitly import an offline bundle.
- `./stack calibrate` — Plan or run report-only local calibration.
- `./stack storage` — Report storage or conservatively garbage-collect temporary files.
- `./stack credentials` — Audit credential metadata without reading values.
- `./stack migrate` — Check schema compatibility; never migrate silently.
- `./stack reference` — Generate this reference from control-plane definitions.

## 稳定退出码

- `0` — 成功
- `2` — 参数或显式批准错误
- `3` — 主机/资源准入拒绝
- `4` — 配置或 schema 错误
- `5` — 外部命令、服务或网络错误
- `6` — 哈希、签名或 bundle 完整性错误
- `7` — 存在未恢复事务或恢复失败

## 结构化结果 schema

所有带 `--json` 的命令输出一个对象：

```json
{"schemaVersion":1,"command":"plan","status":"ok","code":0,"summary":"...","facts":{},"nextActions":[]}
```

`nextActions[].requiresApproval=true` 表示该动作会改变主机状态。

## 本地 schema 版本

- `attestation`: `1`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）
- `bundle`: `1`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）
- `commandResult`: `1`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）
- `runtimeProfiles`: `1`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）
- `transaction`: `1`（当前可读版本；未来版本只读兼容 N-1，禁止静默迁移）

# 质量门禁与发布流程

## 合成质量集

`quality/cases.json` 是可提交、无秘密的固定回归集，覆盖中文与英文指令、简单推理、
信息抽取、代码、JSON 契约和 Tool Use。执行器通过 ModelPort Anthropic Messages
入口调用真实本地模型，确保评估包含协议转换、逻辑模型档位和严格 Tool Use 校验。

```bash
# 日常：4 个关键 Case
scripts/quality-eval.py --smoke

# 升级：全部 Case 重复三次
scripts/quality-eval.py --trials 3

# 定位单项
scripts/quality-eval.py --case tool-weather --trials 3
```

门禁采用可自动判定的精确文本、标记、JSON Schema 子集或 Tool Use 参数断言。证据
保存到被 Git 忽略的 `logs/quality/`，权限 `0600`；只记录 Case ID、类别、通过结果、
延迟和 Token，不保存输入或模型输出。实际业务失败应先脱敏和最小化，再转化为新的
合成 Case，不能直接复制生产 Prompt。

当前基线为 10 个 Case，其中两个 Tool Use Case 都使用 named `tool_choice` 强制单个
工具，只断言工具名和必填字段存在。30/30 表示这组固定协议与质量冒烟稳定，不表示
多工具选择、多步执行或业务 Tool Use 成功率达到 100%。

独立闭环 Tool Harness 已落地为 `quality/tool-workflows.json` 和
`scripts/tool-workflow-eval.py`。五个场景族展开为 40 个固定 Case，覆盖多工具自动选择、
无需工具直答、enum/number、嵌套对象、数组、Unicode 和 Tool Result 后最终答案；其中
5 个进入 standard 冒烟：

```bash
# standard 使用的 5 Case 闭环冒烟
scripts/tool-workflow-eval.py --smoke

# 全部 40 Case；模型升级或协议变更时执行
scripts/tool-workflow-eval.py

# 单项重复定位
scripts/tool-workflow-eval.py --case calculator-add --trials 3
```

每个 Tool Case 都严格匹配工具名和完整参数，执行确定性 Mock Tool，把结果作为
`tool_result` 返回，并要求下一轮给出包含预期事实的最终正文且不得再次误调用。证据仅
保存 Case ID、阶段、轮数、延迟、Token 和通过结果。Mock Executor 只用于验收，不进入
ModelPort 网关或生产应用。后续多步、错误恢复、注入与超长结果范围见
[`ENHANCEMENT_ROADMAP.md`](ENHANCEMENT_ROADMAP.md)。

## 独立候选端口

RTX 5070 Ti 16GB 无法同时驻留两份完整 128K 实例，因此候选采用“不同容器、不同
Compose project、不同端口，串行占用 GPU”的方式。生产配置不会被候选脚本改写：

```bash
# 自动停生产、启动 18081 候选、验收，并无论成功失败都恢复原生产实例
scripts/release-candidate.sh quick

# 额外运行 118K 召回和 92K decode，耗时较长
scripts/release-candidate.sh long
```

手工调试可使用 `candidate-runtime.sh start|accept|status|stop`；启动前必须先停止生产。
候选使用独立的 `cache/candidate/`，不会污染生产 Prompt/KV Cache。生产停止期间，
候选临时接管内部 `qwen-runtime` alias，使 ModelPort、Tool Use、精确 Token、上下文
准入和质量冒烟也测试候选本身，而不只是 `18081` 直连。完整日志以 `0600` 保存到
`logs/releases/`。

## 晋级与回滚

1. 固定候选镜像 digest、模型 SHA256 和单一主要变量。
2. 候选端口依次通过 quick、长上下文、Tool Use、三次质量集和部署余量检查。
   Tool Use Reliability v2 上线后还必须通过完整 Schema 负例和闭环任务集。
3. 更新 `manifest.json`、配置 SHA256、部署记录和决策记录；提交 Git tag。
4. 用正式 `profiles/latency.env` 重建生产容器，运行 `acceptance-suite.sh standard`。
5. 失败时恢复上一 Git tag 和对应 digest，再执行 `runtime.sh profile latency`。

由于模型文件不进入 Git，回滚要求原 GGUF 仍在 `models/` 且 SHA256 匹配；删除旧
制品前必须先完成一次回滚演练。

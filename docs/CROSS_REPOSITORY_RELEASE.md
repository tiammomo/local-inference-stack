# Local Inference Stack × ModelPort 发布契约

两个仓库独立发布，但本地 Qwen Provider 必须作为一个兼容单元验收。机器可读能力边界
由 [`contracts/local-qwen-provider-v1.json`](../contracts/local-qwen-provider-v1.json)
定义；部署身份和镜像由当前硬件档案的 `manifest.json` 固定。

## 配置兼容检查

```bash
python3 scripts/compatibility-check.py \
  --modelport-project /path/to/ModelPort
```

该命令只读比较以下语义，不依赖两个仓库位于相邻目录：

- Provider ID、运行时端点和精确 served model；
- fast/code/deep 别名目标、默认思考开关和预算；
- strict Tool Use、并行调用和有界修复策略；
- 精确 Token Count、128K 硬上限和思考输入建议值。

`acceptance-suite.sh standard` 会自动执行该检查。任一侧只改了配置而没有同步契约时，
联合验收会在发起真实推理前失败。

## 发布状态检查

发布候选还必须执行：

```bash
python3 scripts/compatibility-check.py \
  --modelport-project /path/to/ModelPort \
  --release
```

发布模式额外要求两个工作区均干净，并要求 ModelPort HEAD 与部署 manifest 固定的
`gateway.sourceCommit` 一致。ModelPort 应通过自己的
`scripts/build-container.sh` 构建；该脚本把源码 revision 和 clean/dirty 状态写入 OCI
label。`verify-deployment.py` 会继续把运行中的 label、镜像 ID、容器加固项、配置哈希
和模型 SHA256 与 manifest 对比。

开发阶段可以构建带 `source-state=dirty` 的本地测试镜像，但它只能用于联合验收，不能
更新为正式部署证据。发布顺序固定为：

1. 两个仓库完成代码与文档门禁；
2. 提交 ModelPort，使用干净工作区构建镜像；
3. 更新本项目 manifest 中的 commit、镜像 ID 和配置哈希；
4. 提交本项目并执行 configuration、standard 和 deployment verification；
5. 最后推送两个仓库，避免线上出现单边契约。

验收记录只保存提交、哈希、枚举结果和聚合指标，不保存 Prompt、模型正文、Tool 参数、
API Key 或环境文件内容。

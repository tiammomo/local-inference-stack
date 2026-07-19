# Prompt Cache 与 KV 快照

## 默认缓存

生产默认使用 llama.cpp 8GiB 主机 RAM Prompt Cache。相同 system prompt、工具
Schema、仓库规则和消息前缀可以显著降低长上下文预填充与首轮响应等待；时间戳、RAG 结果和当前问题
应放在尾部。Qwen3.5 当前混合上下文后端不支持非精确 cache reuse，因此不要通过
改写前缀换取表面命中率。

## KV 快照实验入口

运行时已将 `--slot-save-path /cache/slots` 固定到本项目缓存目录。只对完全合成、稳定、
长期复用的前缀做实验：

```bash
scripts/slot-cache.sh list
scripts/slot-cache.sh save synthetic-agent-v1.bin
scripts/slot-cache.sh restore synthetic-agent-v1.bin
```

保存前应先用直连请求把目标前缀加载到 Slot 0，并确认没有生产请求正在运行。恢复只
能用于完全相同的模型 SHA、llama.cpp build、上下文、KV 类型和聊天模板。

KV 快照可能编码或泄露 Prompt 内容，因此视同敏感数据：文件权限设为 `0600`、只保留
在被 Git 忽略的 `cache/slots/`，禁止上传、备份到公共位置或对真实用户会话做快照。
当前不会自动恢复快照；先通过冷/热响应开始时间、真实流式 TTFT、质量集和显存 A/B
后才考虑固定预热流程。

Runtime 固定以宿主 `1000:1000` 身份运行，配合只读根文件系统和已丢弃的 capabilities；
如目标机器使用其他 UID/GID，应在本机环境显式设置 `QWEN_RUNTIME_UID/GID`，不能把
缓存目录改成全局可写。

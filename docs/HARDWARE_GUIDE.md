# 硬件与模型选择

## 推荐原则

模型权重能放入显存不等于服务可用。Catalog 的最低门槛同时给模型权重、CUDA/计算图、
Q8_0 KV Cache、批处理和运行波动留出空间，并校验 RAM 与磁盘。上下文越长、并发
Slot 越多，KV 和临时工作区越大。

自动推荐选择当前主机满足全部最低条件的最大候选。它是可解释的静态规则，不采集
数据、不联网，也不声称预测实际 tokens/s 或质量。

## 当前矩阵

| ID | 最低/建议显存 | 最低 RAM | 权重约 | 初始上下文 | 证据 |
| --- | ---: | ---: | ---: | ---: | --- |
| `qwen35-0.8b-q5km` | 2 / 4GB | 8GB | 0.5GiB | 32K | 估算 |
| `qwen35-2b-q5km` | 4 / 6GB | 12GB | 1.3GiB | 32K | 估算 |
| `qwen35-4b-q5km` | 6 / 8GB | 16GB | 2.9GiB | 64K | 估算 |
| `qwen35-9b-q4km` | 10 / 12GB | 24GB | 5.3GiB | 64K | 估算 |
| `qwen35-9b-q5km` | 14 / 16GB | 32GB | 6.1GiB | 128K | RTX 5070 Ti 已验证 |
| `qwen35-27b-q4km` | 22 / 24GB | 48GB | 15.6GiB | 32K | 估算 |
| `qwen35-35b-a3b-q4km` | 28 / 32GB | 64GB | 20.5GiB | 32K | 估算 |

精确字节数、SHA256 和运行参数只维护在 `catalog/models.json`。文档表格用于解释，
测试会检查 Catalog 结构、边界和路径安全。

## 为什么 16GB 可以是 128K，而更大模型从 32K 开始

当前 9B/Q5 在 RTX 5070 Ti 上已经完成 118K 直连召回、92K 思考链路、Q8_0 KV 和
单 Slot 验收，因此保留 128K 容量。27B/35B 的权重占用显著更高，在尚无实机证据时
先给 32K，优先保证 GPU 余量、思考输出和稳定性。扩容必须逐级做 32K → 64K →
128K 的显存与召回 A/B，而不是只看服务能否启动。

## 特殊机器

- **多 GPU**：工具会显示总显存并给出警告。还需审查每卡容量、PCIe/NVLink、
  tensor split 和最慢卡瓶颈；当前不能自动判定为已验证。
- **CPU-only / Apple Silicon / AMD**：Catalog 的 GGUF 仍可能有用，但本 Compose
  明确是 NVIDIA CUDA，不能直接套用。应新增后端 Profile 和独立验收。
- **共享 GPU**：用“空闲显存”而不是标称显存做容量规划，并禁止自动重启抢占资源。
- **超大内存、较小显存**：部分 CPU offload 可以运行更大模型，但通常损失速度，
  当前的 `gpu-layers=all` 安全基线不会自动选择这种方案。
- **生产多租户**：本项目面向单机可信用户。公网认证、租户隔离和配额应交给网关。

## 新增模型的门槛

1. 确认官方模型身份、许可证和 llama.cpp 兼容性；
2. 审查 GGUF 发布者，固定 HTTPS URL、文件名、精确字节数和 SHA256；
3. 给出保守的 VRAM/RAM/磁盘/上下文起点；
4. 加入边界单元测试；
5. 在目标硬件跑 quick、性能、长上下文和代表性质量集；
6. 只有包含可复查部署记录的条目才能标为 `validated`。

## 上游参考

- [Qwen 官方 llama.cpp 指南](https://qwen.readthedocs.io/en/latest/run_locally/llama.cpp.html)
- [Qwen3.5-9B 官方模型页](https://huggingface.co/Qwen/Qwen3.5-9B)
- [当前 GGUF 发布仓库示例](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF)
- [llama.cpp 官方仓库](https://github.com/ggml-org/llama.cpp)

模型作者与 GGUF 发布者是两个独立信任主体。Catalog 使用 Qwen 模型、Unsloth GGUF
制品与 ggml-org 运行时，三者都应在组织策略下分别审查。

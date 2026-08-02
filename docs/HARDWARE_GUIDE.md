# 硬件与模型

自动推荐面向 Linux/WSL x86_64 NVIDIA 单 GPU，按最大单卡容量、RAM、磁盘和当前
空闲显存综合判断。模型权重能放入显存不代表服务能稳定运行。

| Catalog ID | 最低/建议 VRAM | 最低 RAM | 初始上下文 | 证据 |
| --- | ---: | ---: | ---: | --- |
| `qwen35-0.8b-q5km` | 2/4GB | 8GB | 32K | 估算 |
| `qwen35-2b-q5km` | 4/6GB | 12GB | 32K | 估算 |
| `qwen35-4b-q5km` | 6/8GB | 16GB | 64K | 估算 |
| `qwen35-9b-q4km` | 10/12GB | 24GB | 64K | 估算 |
| `qwen35-9b-q5km` | 12/16GB | 32GB | 128K | RTX 5070 Ti 16GB 已验证；12–16GB 未验证 |
| `qwen35-27b-q4km` | 22/24GB | 48GB | 32K | 估算 |
| `qwen35-35b-a3b-q4km` | 28/32GB | 64GB | 32K | 估算 |

精确大小、哈希和运行参数只以
[`catalog/models.json`](../catalog/models.json) 为准。

`qwen35-9b-q5km` 的 12GB 是部署准入下限，不是已验证的容量结论。当可用显存低于
16GB 时，该配置仍是本机候选项，只有完成 `acceptance-suite.sh quick` 后才能视为
在该主机可用。已记录的实机验证仍是 RTX 5070 Ti 16GB 档位。

验证档案中的 `96GB host RAM` 表示参考机器的物理内存配置，不是 WSL 每次都可见的动态值；
Catalog 可复用硬件档位的 RAM 下限是 64GiB，而 planner 使用当前发行版实际可见/可用内存做准入。
因此 WSL 的内存上限、Windows 占用或重启会改变 plan 读数，不能用物理内存标签替代当前探测。

## 非自动化场景

- 多 GPU：不聚合显存，必须人工设计 tensor split 和独立 Profile。
- CPU、Apple Silicon、AMD：需要不同运行后端和验收。
- 共享/忙碌 GPU：`readyToDeploy=false`，不要绕过空闲显存门禁。
- CPU offload：可能运行更大模型，但性能差异大，不是默认路径。
- 公网或多租户：需要独立认证、隔离、配额和安全评审。

## 将估算升级为已验证

1. 固定模型作者、GGUF 发布者、许可证、revision、大小和 SHA256。
2. 在目标硬件运行 quick、长上下文、性能和代表性质量测试。
3. 记录 GPU/驱动、镜像、Profile、峰值显存和验收结果。
4. 新增 `deployments/<model>-<hardware>/manifest.json` 后再修改 Catalog 状态。

哈希证明制品身份，不证明发布者可信或许可证适用。

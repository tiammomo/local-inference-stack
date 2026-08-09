# 硬件与模型

自动新部署目前只面向 Tier-1 WSL2 x86_64、精确 RTX 5070 Ti 单 GPU 档案，并同时检查
RAM、磁盘和当前空闲显存。native Linux 及其他 NVIDIA 型号在取得真实 qualification 前
只提供诊断。模型权重能放入显存不代表服务能稳定运行。

| Catalog ID | 最低/建议 VRAM | 最低 RAM | 初始上下文 | 证据 |
| --- | ---: | ---: | ---: | --- |
| `qwen35-9b-q5km` | 12/16GB | 32GB | 128K | RTX 5070 Ti 历史实机档案；当前 provisional，禁止新部署 |

精确大小、哈希和运行参数只以
[`catalog/models.json`](../catalog/models.json) 为准。

Catalog 是实机部署 allowlist，不是按显存推测的模型商城。当前唯一条目仍是只读候选；硬件满足
表中容量只会产生诊断，不会授权下载、选择或启动。新增模型或硬件档位必须同时有真实主机、
维护者和当前 qualification，不再凭容量估算加入可执行 Catalog。

条目的 `lifecycleRole=lts` 只表示它占用默认 LTS 槽；`status=provisional` 与
`deploymentEligibility.automatic=false` 才是当前不可自动部署的证据结论。

`qwen35-9b-q5km` 的 12GB 是历史容量候选下限，不是已验证的容量结论。当可用显存低于
16GB 时，该配置仍是本机候选项，只有完成 `acceptance-suite.sh quick` 后才能视为
在该主机可用。已记录的实机验证仍是 RTX 5070 Ti 16GB 档位。

验证档案中的 `96GB host RAM` 表示参考机器的物理内存配置，不是 WSL 每次都可见的动态值；
Catalog 可复用硬件档位的 RAM 下限是 64GiB，而 planner 使用当前发行版实际可见/可用内存做准入。
因此 WSL 的内存上限、Windows 占用或重启会改变 plan 读数，不能用物理内存标签替代当前探测。

## 非自动化场景

- 多 GPU：不聚合显存，必须人工设计 tensor split 和独立 Profile。
- native Linux、其他 NVIDIA 型号：当前只读诊断，需真实 qualification 后才可扩大 Tier-1。
- CPU、Apple Silicon、AMD：不在长期核心支持范围。
- 共享/忙碌 GPU：`readyToDeploy=false`，不要绕过空闲显存门禁。
- CPU offload：可能运行更大模型，但性能差异大，不是默认路径。
- 公网或多租户：需要独立认证、隔离、配额和安全评审。

## 候选 qualification 与晋级

1. 固定模型作者、GGUF 发布者、许可证、revision、大小和 SHA256。
2. quick 只证明当前主机的基本可用性，不能晋级 Catalog；在目标 Tier-1 硬件上从干净
   Git revision 完成结构化 full qualification，包括制品全量重哈希、上下文、性能、质量和恢复。
3. 记录 environment kind、GPU/驱动、镜像、Profile、实际 runtime identity 与逐步结果，
   并确认 `lifecycleRole` 与目标工作流一致。
4. 由外部固定受信 key 对 evidence-derived subject 作 detached signature；晋级时实时验证
   签名、有效期、撤销/替代状态和 `validatedHardware` 与签名硬件事实的一致性。
5. 评审新的 deployment record 后才能把 evidence status 改为 `validated`；只有经过明确
   promotion policy 的 `lts` 条目可以设置 `automatic=true`。

哈希证明制品身份，不证明发布者可信或许可证适用。

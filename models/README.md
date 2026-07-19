# Model artifacts

模型 GGUF 保存在 `models/qwen3.5-9b/`，但不会提交 Git。可复现下载地址、文件名和
SHA256 见 [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)，下载后运行：

```bash
scripts/verify-models.sh --full
```

Model GGUF files live under `models/qwen3.5-9b/` and are intentionally excluded
from Git. See the deployment guide for pinned URLs and SHA256 values, then run
the full integrity verifier before starting the runtime.

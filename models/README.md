# Local model artifacts

GGUF 权重保存在本目录的 Catalog 专属子目录，但永不提交 Git。不要手工猜测目录、
文件名、下载地址或哈希：

```bash
./scripts/model-manager.py plan
./scripts/model-manager.py download --model <catalog-id> --yes
./scripts/model-manager.py verify --model <catalog-id> --cached
```

下载先写入可续传的 `.part`，通过精确字节数和 SHA256 后才原子发布。完整来源与哈希
位于 `catalog/models.json`。

GGUF artifacts live in catalog-specific subdirectories and are excluded from
Git. Use `model-manager.py`; do not create unpinned download instructions.

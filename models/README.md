# Model artifacts

权重保存在此目录但不进入 Git。只使用 Catalog 驱动的管理器：

```bash
./scripts/model-manager.py plan --json
./scripts/model-manager.py download --model <catalog-id> --yes
./scripts/model-manager.py verify --model <catalog-id> --cached
```

不要手工添加 URL、文件名或哈希；第三方模型和许可证仍需独立审查。

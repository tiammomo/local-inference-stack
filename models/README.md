# Model artifacts

权重保存在此目录但不进入 Git。普通用户只使用稳定的 `./stack` 入口：

```bash
./stack plan --json
./stack deploy --model <catalog-id> --yes
./stack verify --scope model --model <catalog-id> --cached
```

不要手工添加 URL、文件名或哈希；第三方模型和许可证仍需独立审查。

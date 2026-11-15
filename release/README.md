# release/ 已归档

完整包不再放在源码树里（每个 1~8 MB，历次共 40 MB），
统一归档在 **`resources` 分支**（tag `resources-2026-11`）。

取用方法见 [`docs/资源归档.md`](../docs/资源归档.md)，例如：

```bash
git fetch origin resources
git show resources:releases/pulses-easier-v0.11.2-full.zip > pulses-easier-v0.11.2-full.zip
```

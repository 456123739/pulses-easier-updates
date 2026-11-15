# resources —— 资源归档分支

这个分支**不是代码**：只放体积大、平时用不到、但需要长期保留的资源，让 `main` 保持轻量。

* 人工索引：[`INDEX.md`](INDEX.md)
* 机器可读清单：[`manifest.json`](manifest.json)（逐文件 sha256）
* 快照标签：`resources-2026-11`（构建产物 + 日志）、`resources-2026-11.1`（追加 dev-ui 界面证据）

## 目录

| 路径 | 内容 |
|---|---|
| `releases/` | v0.2.3 – v0.11.2 的历史完整包（与 GitHub Releases 同一批文件） |
| `logs/` | 各轮测试 / e2e / 真实环境验证日志 |
| `artifacts/2026-11-08-dev-ui/` | 开发者端 UI 重做的界面截图、交付补丁与交付说明 |

## 取用

```bash
git fetch origin resources

# 取单个文件
git show resources:releases/pulses-easier-v0.11.2-full.zip > pulses-easier-v0.11.2-full.zip

# 整目录取到工作区
git checkout resources -- artifacts/2026-11-08-dev-ui

# 只要清单
git show resources:manifest.json | jq '.items[] | select(.group=="artifacts")'
```

不需要完整历史时，浅克隆最省时间：

```bash
git clone --depth 1 --branch resources https://github.com/456123739/pulses-easier-updates.git resources
```

## 追加新资源

1. 放到 `<分组>/<日期>-<主题>/` 下（分组用 `releases` / `logs` / `artifacts`）。
2. 在 `manifest.json` 追加条目（`path` / `group` / `bytes` / `sha256` / `mtime` / `purpose`），
   同步更新 `INDEX.md` 的表格与合计。
3. 提交后打新标签 `resources-YYYY-MM-DD`，旧标签不动。
4. 不进这个分支的东西：可重建的依赖（`node_modules`、`.venv`）、构建中间产物（`dist/`、`__pycache__`）。

## 约束

* 单文件 < 90 MB：GitHub 单 blob 上限 100 MB，超过请放 Releases 或改用 Git LFS。
* 这个分支不参与 `main` 的合并；需要某个文件时按上面的命令取，不要 cherry-pick。
* 主分支 `main` 的 `.gitignore` 已忽略 `release/`，归档文件不会再回到源码树。

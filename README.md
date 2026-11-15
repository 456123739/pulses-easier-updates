# resources —— 大文件归档分支

这个分支**不是代码**，只放历史构建产物与测试日志，用来把 `main` 保持轻量。

* 上一级说明：[`INDEX.md`](INDEX.md)
* 机器可读清单：[`manifest.json`](manifest.json)
* 取用方法：`git fetch origin resources` 然后 `git show resources:<路径> > 本地文件`

主分支 `main` 的 `.gitignore` 已经忽略 `release/`，所以这些文件不会再回到源码树。

# Pulses Easier

Minecraft 整合包更新管理工具（开发者端 + 玩家端），界面基于 CustomTkinter，
目标平台 Windows 10。

> 本仓库是 **下载引擎 v0.2.2 更新版**。

## 下载

* 只需替换文件 → 下载更新包（无需登录）：
  [`release/pulses-easier-download-engine-v0.2.2.zip`](release/pulses-easier-download-engine-v0.2.2.zip)
* 本次改动明细 → [`CHANGELOG.md`](CHANGELOG.md)

更新包内按项目原有目录结构组织，解压后直接覆盖即可：

```
app/core/downloader.py                  ← 下载引擎主体
app/core/database.py                    ← 新增配置项默认值
app/core/__init__.py                    ← 版本号 0.1.0 → 0.2.2
app/ui/main_window.py                   ← 新增启动 boot 任务：探测缓存盘类型
app/ui/widgets/preferences_dialog.py    ← 新配置项的类型登记
app/ui/player_view.py                   ← 槽位面板收起回调桥接
app/ui/widgets/slot_panel.py            ← 标题栏 + 收起按钮
CHANGELOG.md
```

## 运行

```bash
pip install -r requirements.txt
python main.py
```

## 本次更新做了什么

`app/core/downloader.py` 保持原有架构（两级槽位池 + 分片直写 + 快速筛除），
只做增量修复与优化。对外接口一个都没有改动，`player_view.py` 的调用方式不变。

**修复的 5 个正确性缺陷**

| 缺陷 | 后果 |
|---|---|
| 多源回退失效 | `task.urls` 第 2 个及以后的源**从不被尝试** |
| 不校验 HTTP 状态码 | 404/403 错误页会被当成文件写盘 |
| 分片不校验 Range | 源忽略 Range 时**静默写出错位文件** |
| 预分配 `.part` 被当成成品 | 任务无哈希时**零填充文件被判为下载成功** |
| `select` 读空闲误判 | 小文件白等一个超时；chunked 源**必然失败** |

**5 项性能 / 健壮性改进**：中止不再排空响应体、共享 SSLContext、
分片进度聚合（单调不回退）、单连接边下边算哈希 + `posix_fallocate` +
陈旧 keep-alive 透明重试、返回语义与调用方统计口径对齐。

**按磁盘类型限制并发（v0.2.2）**：自动探测缓存目录落在 SSD / 机械盘 /
网络盘 / 可移动盘上，按类型给「并发文件数」和「单文件分片线程数」设上限，
**只能调低不能调高**。机械盘默认压到 4 个文件、且不分片——机械盘是寻道瓶颈，
多开文件和多段随机写都会明显变慢。探测失败一律不干预，可用
`disk_type_override` 手工指定。

**实测（与原版成对 A/B，本地模拟源）**

| 场景 | 原版 | 改进版 |
|---|---|---|
| HTTP 12×24KB（8 槽位） | 6.072s | **0.076s（79.6×）** |
| 3 个文件，双方出厂默认参数 | 61.57s | **0.097s（638×）** |
| HTTPS 24×8KB（8 槽位） | 10.156s | **1.091s（9.3×）** |
| 换源 `[404, 正常源]` ×10 | 成功 0/10 | **成功 10/10** |
| 预分配零填充 `.part` | ❌ 内容损坏却判成功 | ✅ 重新下载、内容正确 |

**真实网络实测（Modrinth 10 + CurseForge 10，69.0 MB）**

用项目自己的 `differ` 分类 + `player_view._collect_download_tasks_from_diff`
生成任务，再逐文件 SHA1 校验：

| 方式 | 耗时 | 吞吐 | 校验 |
|---|---|---|---|
| 裸 urllib 顺序单线程 | 567.91s | 0.12 MB/s | **18/20**（2 个超时彻底失败） |
| 原版 downloader | 156.22s | 0.44 MB/s | 20/20（靠"大小够就算下完"的危险捷径） |
| **新版 downloader** | **22.59s** | **3.05 MB/s** | 20/20 |

**C/A = 25.1×，C/B = 6.9×**。另跑 3 轮"轮转顺序 + 网络基准归一化"配对回合
（8 个文件）：归一化中位 **C/A ≈ 8.6×、C/B ≈ 11.2×**，三轮顺序轮换后 C 始终最快。

## 测试

```bash
python3 tests/test_basic.py        # 基础：编译 / 导入 / 启动链路 / 配置一致性（20 项）
python3 tests/test_downloader.py   # 功能：调度 / 换源 / 超时 / 分片分级 / 进度 / 中止 / 磁盘策略（51 项）
python3 tests/bench_ab.py          # 与原版的成对 A/B 实测（本地模拟源，约 3.5 分钟）
python3 tests/realworld_test.py    # 真实网络实战：Modrinth 10 + CurseForge 10（会下载约 70MB，跑完自动删除）
```

* 基础测试 20/20、功能测试 51/51 通过。
* 基础测试用桩依赖把 `main.py` 的启动链路整条跑通（组类 → 构造主窗口 →
  双端预热 → boot 任务），但**不覆盖真实 Tk 渲染**——那部分仍建议在
  Windows 10 实机上点一遍。
* 原始日志见 `tests/logs/`。
* `tests/_baseline/downloader_original.py` 是改动前的原文件逐字副本，
  供 A/B 复现；`tests/_tls/` 是本地 HTTPS 测试用的自签证书。

## 已知限制 / 后续建议

* 更新检查建议改用批量接口：Modrinth `POST /v2/version_files/update`
  （实测 7 个文件 1 次请求 0.87s，对比逐个 GET 9.16s/4 个文件）、
  CurseForge `POST /v1/fingerprints` + `POST /v1/mods`。属于上层逻辑，本次未动。
* 机械盘建议把「并发文件数」压到 4 左右（SSD 实测并发文件数 24 时写入吞吐
  反而下降到 ~70%）。
* 不要引入逐块 `fsync`（实测 3.15ms/次，等效吞吐只有 ~31 MB/s）。

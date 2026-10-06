# Pulses Easier

Minecraft 整合包更新管理工具（**开发者端 + 玩家端**），界面基于
CustomTkinter，目标平台 Windows 10。

> 下载引擎：**RideX（锐驰引擎）** —— 多源并发、多分片、多级重试，
> 按目标磁盘类型自动限制并发。
>
> 当前版本 **v0.5.0（整包发布）**。
> 从本版本起不再提供"替换几个文件"的增量包 —— 整个项目按原目录结构打包，
> 解压即是完整可用工程。改动明细见 [`CHANGELOG.md`](CHANGELOG.md)，
> 审查与修复状态见 [`docs/Easier-流程审查报告.md`](docs/Easier-流程审查报告.md)。

## 下载

* **完整项目包（推荐）**：
  [`release/pulses-easier-v0.5.0-full.zip`](release/pulses-easier-v0.5.0-full.zip)
  —— 解压后可直接 `python main.py`。
* 历史增量包（只含 8 个文件，v0.3.0 下载引擎聚合更新）：
  [`release/pulses-easier-download-engine-v0.3.0-aggregate.zip`](release/pulses-easier-download-engine-v0.3.0-aggregate.zip)

## 运行

```bash
pip install -r requirements.txt
python main.py
```

首次启动会引导你选择**数据库目录**（放配置与缓存的地方），随后：

* **玩家端**：拖入整合包内任意文件 → 定位整合包 → 拖入更新包
  （`.eapack` / `.zip`）→ 开始更新 → 比对 → 确认更新（下载 + 应用）。
* **开发者端**：拖入启动器导出的整合包 ZIP / `.mrpack` / `.eapack`
  → 调整策略与黑名单 → 写更新日志 → 设置导出选项 → 开始制作更新包。

## 目录结构

```
main.py                     启动入口
app/
  config.py                 内容类型 / 策略枚举 / 默认策略（唯一事实来源）
  theme.py                  颜色 / 字体 / 尺寸
  core/
    apply_rules.py          「这条变更会不会被应用」的唯一判定（三处共用）
    differ.py               新旧版本比对（文件级 + 文件夹级）
    downloader.py           RideX 锐驰引擎（多线程槽位池 + 并行耐心池 + 分片直写）
    updater.py              按策略执行更新（失败时事务性回滚）
    mrpack.py               modrinth.index.json / overrides 解析
    eapack.py               更新包（.eapack）导出与完整性校验
    hashing.py              哈希清单
    checkpoint.py           更新阶段检查点（用于发现"上次没跑完"）
    resume.py               下载中断恢复记录
    cache.py                缓存与工作目录清理
    database.py             数据库路径与配置读写（原子写）
    pack_locator.py         根据拖入的文件向上定位整合包
    pack_info.py            整合包信息（名称/版本/图标/模组数）
    pack_detector.py        拖入 ZIP 的类型识别
    markdown_renderer.py    更新日志渲染（含独立预览窗口）
    preview_worker.py       预览子进程入口
    recent.py               最近打开的整合包
    state.py                应用级状态（锁定 / 当前整合包）
  ui/                       主窗口、侧边栏、玩家端、开发者端
  ui/widgets/               策略表、变更列表、下载面板、槽位面板等
  utils/                    文件与动画等通用工具
assets/                     图标等资源
tests/                      测试与夹具（无 GUI 环境下的桩、本地 HTTP 夹具）
docs/                       流程审查报告与修复状态
release/                    发布产物
```

## v0.5.0 做了什么

三批修复，来源是对玩家端/开发者端**全流程**的代码审查：

1. **止血（P0×6）**：非白名单目录不再被整体删除（比对去掉 mtime +
   默认策略改"替换重名" + 事务性目录替换）；更新包元数据不再被写进
   整合包根目录；下载阶段真正加锁；应用阶段重入保护；
   下载部分失败有终态出口；单文件直通应用也有强关警告。
2. **闭环（P1×12）**：检查点用于发现"上次没跑完"；校验升级为内容级；
   失效的 resume 记录不再被删除（"重新定位更新包"复活）；
   换包/换整合包强制作废旧比对；缓存目录带指纹且只并入本次任务的文件；
   工作目录移出系统临时目录并在退出时清理；成功后清 resume；
   失败文案不再说"完成"；扫描期快照化；index 声明的任意路径都能应用；
   非法路径丢弃。
3. **整洁（P2）**：`apply_rules` 收敛三处重复判定；根目录散文件按内容比对；
   死配置项要么接上要么删除；单连接续传也校验 `Content-Range`；
   嵌套 `overrides/`；删除 5 个死模块；开发者端一串修复；
   导出原子写 + 可校验的完整性签名。

**设计取向**：更新是**向前收敛**的（比对按当前状态重算，重新拖入同一个
更新包再更新一次永远安全），因此**不提供"撤销上次更新"**；
只保留失败时的事务性回滚。默认策略也**不删玩家自己写的文件**。

## RideX（锐驰引擎）v0.3.0 改动（历史）

`app/core/downloader.py` 保持原有架构（两级槽位池 + 分片直写 + 快速筛除），
只做增量修复与优化；对外接口一个都没有改动。

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

**按磁盘类型限制并发**：自动探测缓存目录落在 SSD / 机械盘 / 网络盘 /
可移动盘上，按类型给「并发文件数」和「单文件分片线程数」设上限，
**只能调低不能调高**。机械盘默认压到 4 个文件且不分片——机械盘是寻道瓶颈，
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
python3 tests/test_basic.py        # 基础：编译 / 导入 / 启动链路 / 配置一致性
python3 tests/test_downloader.py   # 功能：调度 / 换源 / 超时 / 分片分级 / 进度 / 中止 / 磁盘策略
python3 tests/test_fixes.py        # 第一批：止血修复回归
python3 tests/test_devpack.py      # 第二批：闭环回归
python3 tests/test_polish.py       # 第三批：健壮性与整洁回归
python3 tests/e2e_flow.py          # 端到端（会真实下载一个 Modrinth 模组）
python3 tests/bench_ab.py          # 与原版的成对 A/B 实测（本地模拟源，约 3.5 分钟）
python3 tests/realworld_test.py    # 真实网络实战：Modrinth 10 + CurseForge 10（约 70MB，跑完自动删除）
```

全部套件共 **218 项**检查通过（32 + 51 + 26 + 23 + 48 + 38）。

* 基础测试用桩依赖把 `main.py` 的启动链路整条跑通（组类 → 构造主窗口 →
  双端预热 → boot 任务），但**不覆盖真实 Tk 渲染**——那部分仍建议在
  Windows 10 实机上点一遍。
* `tests/_baseline/downloader_original.py` 是改动前的原文件逐字副本，
  供 A/B 复现；`tests/_tls/` 是本地 HTTPS 测试用的自签证书。

## 已知限制 / 后续建议

* 开发者端没有"取消导出"与关闭守卫：导出已改为原子写，中途关闭只会留下
  一个 `.part-eapack` 临时文件，不会产生半成品更新包。
* 更新检查建议改用批量接口：Modrinth `POST /v2/version_files/update`
  （实测 7 个文件 1 次请求 0.87s，对比逐个 GET 9.16s/4 个文件）、
  CurseForge `POST /v1/fingerprints` + `POST /v1/mods`。属于上层逻辑，本次未动。
* 机械盘建议把「并发文件数」压到 4 左右（SSD 实测并发文件数 24 时写入吞吐
  反而下降到 ~70%）。
* 不要引入逐块 `fsync`（实测 3.15ms/次，等效吞吐只有 ~31 MB/s）。

## 许可

内部工具，未附带开源许可证。

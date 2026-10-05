# Pulses Easier 更新日志

## v0.2.3 — 下载引擎增量改进 + 磁盘策略 + 调度/分片细化 + 失败交还（2026-10-05）

本次只改下载链路与它直接相关的配置，**不重写既有架构**：
两级槽位池（多线程槽位 → 单线程重试队列）、分片直写、快速筛除(B2) 全部保留。
所有对外接口（`download_files` 的参数、`DownloadTask` / `DownloadResult`
的字段、`estimate_eta` / `invalidate_options`）**一个都没动**，
`player_view.py` 无需改动调用方式。

---

## 一、需要替换的文件

| 文件 | 归属 | 说明 |
|---|---|---|
| `app/core/downloader.py` | 本次改动 | 下载引擎主体：5 个缺陷修复 + 5 项性能改进 + 磁盘策略 + 调度/分片细化 |
| `app/core/database.py` | 本次改动 | 新增配置项默认值与首选项说明（纯追加，旧库自动兼容） |
| `app/ui/widgets/preferences_dialog.py` | 本次改动 | 新配置项的类型登记（整数/浮点） |
| `app/ui/main_window.py` | 本次改动 | 新增启动 boot 任务「探测缓存盘类型」 |
| `app/ui/widgets/download_panel.py` | 本次改动 | 失败项补入：复制/打开链接、多源显示、拖入匹配放宽与失败提示 |
| `app/core/__init__.py` | 本次改动 | 版本号 `0.1.0` → `0.2.3` |
| `app/ui/player_view.py` | 你方最新版 | 槽位面板「收起」回调桥接（`on_toggle`） |
| `app/ui/widgets/slot_panel.py` | 你方最新版 | 标题栏 + 收起按钮，回调 `on_toggle(False)` |

> 后两个是本次上传的最新版原样收录，方便一次性对齐；如果你那边已经是最新版，
> 可以只替换前四个。

文件校验（md5）：

```
da793a8163425203d3388432f465807d  app/core/downloader.py
724c2a87de52e6372b3b0b71a03c3297  app/core/database.py
909872fcf4080f512a890f99d627ed35  app/core/__init__.py
cbeed113bf34c6e354c12e1a9b301863  app/ui/main_window.py
4cde25f19b606feb1ff0e7dc9368ce57  app/ui/widgets/download_panel.py
6d1e08f2849778217ea15f137b7ab3d5  app/ui/widgets/preferences_dialog.py
b1a187b30a064dd5774ff663384fc96d  app/ui/player_view.py
a3aa5f7cc067736df87f8c63ea052e46  app/ui/widgets/slot_panel.py
```

---

## 二、修复的缺陷（正确性，P0）

### G1 多源回退失效 —— 第 2 个及以后的下载源永远不会被尝试

* **现象**：`task.urls` 里第 2 个源从来没被请求过，官方源挂掉就直接判失败。
* **原因**：`_attempt_file_inner` 里 `single_fail_count` 在 URL 循环**外面**
  初始化，第 1 个 URL 用尽 2 次配额后 `break` 直接退出整个 `for url in ...`
  循环；循环体末尾两个分支都是 `break`。
* **修复**：每个候选 URL 独立执行「单连接 N 次 → 分片一次」，全部失败才判失败；
  `retry_same_url` 从死配置接回（默认值 1 → 每 URL 2 次尝试，与旧行为一致）。
* **实测**：10 个文件、每个 `[404, 正常源]` —— 原版 **成功 0/10**，改进版 **10/10**。

### G2 不校验 HTTP 状态码 —— 404/403 错误页会被当成文件写入磁盘

* **原因**：`_open_url_stream` 只处理重定向，其余状态码一律把 `resp` 交给下载循环。
* **修复**：非 2xx 抛 `_HttpStatusError`，区分 `retry`（408/425/429/5xx，可重试）
  与 `dead`（404/403 等，该链接作废换下一个源），只读取 ≤512B 错误页摘要进日志，
  连接不复用。`http_status_check=0` 可关闭。

### G3 分片不校验 Range —— 源忽略 Range 时会静默写出错位文件

* **原因**：分片请求直接 `seek(start)` 写入，从不检查返回的
  `Content-Range` / 状态码；服务器忽略 Range 时把整份文件写到错误偏移上，
  大小却"正好"对得上，只有哈希能发现。
* **修复**：分片必须 `status == 206` 且 `Content-Range` 的区间与请求一致
  （无该头时退化为 Content-Length 精确匹配），否则抛
  `_RangeUnsupportedError` → **整份作废并换源**，绝不产出损坏文件。
  另有 416 专门处理"远端长度不足"。

### G4 预分配出来的 `.part` 被当成"已下载完成"（数据损坏）

* **现象**：任务没有哈希（只有 `file_size`）时，下载结果"成功"但文件内容
  是一整片零。
* **原因**：分片下载会把 `<target>.part` 预分配成和最终文件一样大，
  而 `_try_single_url` 的判断是 `existing_size >= task.file_size`
  → 直接 `part.replace(target)`，零填充文件被当成品，且 `_verify` 在
  没有哈希时只比大小，照样通过。`file_size == 0`（未知大小）时该条件
  恒真，任何残留 `.part` 都会被当成品。
* **修复**：引入 `<target>.part.meta` sidecar 记录**确实写完的字节区间**；
  续传只认「从 0 开始的连续前缀」，没有 sidecar 的旧 `.part` 一律重下；
  "已下全"的判定同时要求 sidecar 覆盖 `[0, file_size)`。
  `part_meta_enabled=0` / `part_meta_trust_size=1` 可退回旧口径（不推荐）。
* **实测**：预置一个零填充的满长 `.part` —— 原版 **判为成功且内容是零**，
  改进版 **重新下载、内容正确**。

### G5 读空闲误判 —— 数据已在用户态缓冲，`select` 却看不见

* **现象**：小文件在 keep-alive 连接上要**干等一个超时**才完成；
  耗时随 `read_idle_timeout` 线性增长（默认 20 秒/文件）；
  无 `Content-Length`（chunked）的源**必然失败**。
* **原因**：`_stream_to_file` 用 `select()` 探测**原始 socket**，而
  `http.client` 的 `BufferedReader` 已经把响应体预读进用户态缓冲区，
  `select` 认为"没数据"，于是走 `stalled` 分支。
* **修复**：删掉 `select` 探测，改为 **socket 超时轮询 + `HTTPResponse.read1()`**：
  每次读超时回到循环顶部，重新评估 `stall_timeout` / `per_url_timeout` /
  速度窗口 / 用户中止。`read_poll_interval`（默认 1.0s）控制轮询粒度。
* **实测**：3 个小文件、双方都用出厂默认参数 —— 原版 **61.57s（20.52s/文件）**，
  改进版 **0.095s（0.032s/文件）**；chunked 源原版 **失败（12.1s）**，
  改进版 **0.0s 成功**。

> ⚠️ 顺带修掉一个由本次改造引入的问题：`read1()` 读完正文时**不会**像
> `read()` 那样把响应标记为 closed，导致下一次 `getresponse()` 抛
> `ResponseNotReady`、keep-alive 复用彻底失效（实测 12 个文件 = 12 条连接，
> 且每个文件都要重连一次）。现在统一在归还响应时显式 `resp.close()`，
> 实测 12 个文件 = **1 条连接、12 个请求**。

---

## 三、性能与健壮性改进

| 编号 | 改动 | 说明 |
|---|---|---|
| G6 | 中止/停滞时不再排空响应体 | `_release_response()` 取代无条件 `resp.read()`：读完才留连接，没读完直接断开。旧实现在用户点"取消"后还要把服务器剩余响应体读完，最坏要等一个 `read_idle`。 |
| G7 | 共享 `ssl.SSLContext` | 旧实现走 `HTTPSConnection(context=None)`，**每条连接**都新建 SSLContext 并重载 CA 信任库（实测 ~48ms/连接，多线程下被 GIL 串行化）。改为进程内共享一个 context（`shared_ssl_context`，默认开）。 |
| G8 | 分片进度聚合 | 旧实现 `byte_cb` 上报的是**单片**字节数（0→片大小循环），并发分片时界面进度会反复回退。改为 `_FileProgress` 聚合「已完成 + 各在途分片」，对外单调、且带高水位夹紧。 |
| G9 | 单连接边下边算哈希；`posix_fallocate` 预分配；陈旧 keep-alive 透明重试；指数退避+抖动 | 从 0 开始下载时用 `hashlib` 边下边算，省掉收尾的一次全文件重读（`hash_while_streaming`，默认开；续传时自动退回整文件校验）。分片预分配优先 `os.posix_fallocate`，不可用（典型：Windows）时回落 `truncate`。 |
| G10 | 返回语义与调用方对齐 | 旧实现 `results` 只装"非 ok"的项，而 `player_view` 按 `成功 = len(results) - len(failed)` 统计、并靠 `if r.ok: _completed_files.append(...)` 收集已完成文件 —— 结果是**成功数永远是 0、已完成列表永远收集不到东西**，而且"先失败后被单线程池救回"的文件还会在列表里留一条失败记录。现在每个任务返回**恰好一条最终结果**；进度只在最终态推进（`done` 不会超过 `total`）；`on_file_failed` 只在最终失败时触发。 |
| G11 | 按磁盘类型限制并发 | 见下面 §3.1。 |

---

## 三·一、G11：按磁盘类型限制并发（本轮新增）

### 为什么

SSD 上「多开文件 + 单文件分片」几乎线性收益，机械盘却是**寻道**瓶颈：
同时写多个文件、或对同一文件做多段随机写，都要让磁头来回跑。一次寻道 5~15ms，
是 SSD 的上百倍。本机 SSD 上就已经能看到拐点：并发文件数 4→12 时写入吞吐
87→106 MB/s，但 12→24 时反而掉到 74 MB/s；机械盘上这个拐点低得多。

### 怎么探测（任一步成功即返回；任何异常都不会抛出）

| 平台 | 顺序 | 判定 |
|---|---|---|
| Linux | `/proc/mounts` 最长前缀匹配挂载点 | `nfs`/`cifs`/`smb3`/`sshfs`/`fuse.rclone`/`9p`… → **network**；`iso9660`/`udf` → **removable** |
| Linux | `/sys/class/block/<dev>/queue/rotational` | `1` → **hdd**，`0` → **ssd**；分区名自动回溯到整盘（`sdb2→sdb`、`nvme0n1p2→nvme0n1`），`dm-*`/`md*` 看 `slaves`（任一层是机械盘就按机械盘对待） |
| Windows | `GetDriveTypeW` | `DRIVE_REMOTE(4)` → **network**；`DRIVE_REMOVABLE(2)`/`DRIVE_CDROM(5)` → **removable** |
| Windows | `IOCTL_STORAGE_QUERY_PROPERTY` + `StorageDeviceSeekPenaltyProperty` | 以 0 访问权限打开卷句柄查询，**普通用户即可**，不需要管理员；`IncursSeekPenalty=1` → **hdd** |
| Windows | PowerShell `Get-Partition \| Get-Disk` 的 `MediaType` 兜底 | 仅在上一级失败且 `disk_probe_fallback=1` 时执行，带超时；可用 `disk_probe_fallback=0` 关掉 |

结果按真实路径缓存，一个进程只探测一次。

### 策略（**只能调低，不能调高**）

| 磁盘类型 | 并发文件数上限 | 单文件分片线程上限 |
|---|---|---|
| `hdd` | `hdd_max_files` = **4** | `hdd_part_threads` = **1（即不分片）** |
| `network` | `network_max_files` = **6** | `network_part_threads` = **1** |
| `removable` | `removable_max_files` = **2** | `removable_part_threads` = **1** |
| `ssd` | `ssd_max_files` = 0（不额外限制） | `ssd_part_threads` = 0（不额外限制） |
| `unknown` | 不干预 | 不干预 |

* 生效值 = `min(用户配置, 类型上限)`，绝不会把用户调小的值抬回去。
* 分片线程被压到 1 时，`_attempt_file_inner` **直接跳过整条分片路径**
  （不做无意义的预分配和随机写），大文件走顺序单连接。
* 显式传入的 `threads=` 参数优先于并发文件数上限（分片线程上限始终生效）。
* 探测结果与最终生效值会写进日志：
  `磁盘类型判定为 hdd（…）→ 并发文件数 16→4，单文件分片线程 4→1`
* 探测不准（虚拟盘、RAID、网盘挂载）时可以用 `disk_type_override`
  直接指定 `ssd`/`hdd`/`network`/`removable`/`unknown`。
* 公开了两个可直接调用的接口，UI 以后想显示也能用：
  `downloader.resolve_disk_policy(dir, opts)` 与
  `downloader.disk_policy_summary(dir, opts)`。

---

## 三·二、失败文件的"交还给用户"链路（本轮补齐）

这条链路本来就有，不需要新做——`player_view` 在最终失败时把
`(rel_path, urls, error)` 交给 `DownloadPanel`，面板列出「文件名 + 失败原因 +
下载链接」，底部是拖入框，`_on_file_dropped` 会把用户下到的文件复制到
`<cache>/<rel_path>` 并用 `downloader._verify` 校验，通过才计入已完成；
「全部跳过」则直接进入应用阶段。

但读代码发现 **3 个真实缺口**，其中第 1 个直接废掉了"把链接甩给用户"的意图：

| 缺口 | 现象 | 修法 |
|---|---|---|
| 链接抄不走 | 链接只是一个 `CTkLabel`——不可选中、不可点击、没有复制按钮，用户对着 80 多字符的 CDN 地址只能手打 | 每行加「复制链接」「打开链接」两个按钮；复制内容把所有源都带上（首行附文件名便于对照），打开用系统浏览器 |
| 多源只显示第一个 | `urls[0]`，备用镜像站用户根本看不到 | 显示"共 N 个源，复制会带上全部" |
| 拖入匹配太严且失败无提示 | 只做 `p.name == Path(rel).name` 精确比对。浏览器下载会自动加 `(1)`/`- 副本` 后缀 → 匹配不上；且**匹配不上时没有任何反馈**，用户以为拖进去了其实什么都没发生 | 归一化匹配（容忍副本后缀、忽略大小写），同名多目录会逐个尝试；仍匹配不上/校验不过时通过 `on_notice` 明确告知 |

归一化刻意**不**削版本号：`create-1.20.1-6.0.7.jar` 必须原样保留，
只认 `(1)` / `- 副本` / `- copy` 这几种明确的副本标记（有测试守着）。

另一个兜底规则：当待补入项**只剩一个**时，拖进来的文件直接归给它——
这是安全的，因为 `_on_file_dropped` 仍会走 `_verify`，对不上会被拒。

**验证**：`TestDownloadPanel` 12 项，覆盖版本号不被削、副本后缀被削、
多源去重、复制内容含文件名与全部源、同名多目录逐个尝试、单件兜底、
多件时不乱认、成功移除条目、不匹配要给提示、校验不过要保留并提示。

**与本次下载器改动的配合**：G10 把 `on_file_failed` 收敛为**只在最终失败时**
触发，正好是面板要的语义——以前"先失败、后被单线程池救回"的文件会被误报进
失败面板，现在不会了。

---

## 三·三、调度与分片的三处调整（本轮）

### G12 耐心重试不再是一条串行尾巴

**原来**：三级池是严格串行的——多线程槽位池**全部跑完**，才开始单线程重试池。
一个文件在主池里早早失败，却要等到最后一个正常文件下完才被重试；
这期间它什么都没有做，纯粹是尾巴。20 个文件里 1 个需要耐心重试、
重试花 30 秒，总耗时就凭空多 30 秒。

**现在**：保留三级池的**职责划分**（激进档 / 耐心档），但把耐心池**提前开**：
主池里每有一个文件降级，立刻提交给耐心池，与"还没下完的正常文件"重叠执行。

* 耐心档的并发仍然由 `single_slots`（默认 4）封顶 → 不会出现 12 个槽位
  全被卡住的文件占满；
* 挂起连接数上限 = `multi_slots + single_slots` = 16，远低于
  `max_connections = 128`；
* 主池空掉之后再收尾扫一次队列，保证不漏；
* 日志从「N 个文件进入单线程重试阶段」改成
  「N 个文件进入耐心重试（与主池并行，最多 4 个同时）」。

**验证**：`TestPatientOverlap.test_patient_retry_overlaps_main_pool` 用
"12 个任务、6 槽位、第 0 个文件在主池里 20ms 秒失败"构造场景，断言
**耐心重试的开始时刻早于主池最后一个任务的结束时刻**（旧实现必然晚于）。

### G13 磁盘类型改为启动时探一次

**原来**：`download_files` 每次调用都走一遍 `resolve_disk_policy`。虽然
`detect_disk_type` 有按真实路径的缓存，但第一次仍要付探测成本——Windows 上
`IOCTL` 失败走 PowerShell 兜底最坏 2 秒，刚好卡在"点开始下载"到"第一字节"
之间。

**现在**：下载目标固定是 `<database>/cache`，启动时就知道，所以

* `main_window.get_boot_tasks()` 增加一条 boot 任务「探测缓存盘类型」，
  在启动页的后台线程里调一次 `downloader.probe_disk_cache(cache_root)`；
* 结果被 downloader 缓存，之后每次下载只做一次字典命中，不再碰
  `/proc/mounts`、sysfs（Windows 上则是 IOCTL / PowerShell）；
* 启动完成后把结论写进日志（`disk_policy_summary`），用户能看到实际判定；
* 新增 `probe_disk_cache()` / `clear_disk_cache()` 两个接口，
  换盘或换数据库目录时可以手动刷新。

**验证**：`test_probe_disk_cache_warms_and_skips_reprobe` 包一层计数器，
断言"启动探 1 次 + 之后连续 5 次 resolve"总共只探测 **1 次**。

### G14 分片按大小分级（明确**不**做动态拆分）

**原来**：一刀切——`>= 4 MiB` 就用固定 4 片，超过多少都一样。

**现在**：分片数按文件大小**静态**算出来，启动前就确定、零协调成本：

```
< multi_part_min_bytes            → 1（不分片）
否则 ceil(size / target_part_size) → 夹到 [2, max_part_count]
```

默认（4 MiB / 8 MiB / 8 片）的实际结果：

| 文件大小 | 分片数 | 每片 |
|---|---|---|
| 1 MiB / 3 MiB | 1 | 不分片 |
| 4 MiB / 8 MiB / 16 MiB | 2 | 2 / 4 / 8 MiB |
| 20 MiB | 3 | 6.7 MiB |
| 64 MiB 及以上 | 8（上限） | ≥8 MiB |

另外新增 `large_file_multi_first_bytes`（默认 `0` = 关闭）：设成非 0 时，
超过该大小的文件会**先试分片**再退回单连接。默认保持"单连接优先"。

**为什么不做运行中动态拆片**（即"发现某片慢就把它对半拆开"）：

1. **收益本来就小**：本机对 12~27 MB 文件实测多连接只有 **1.02~1.33×**；
   而这次 20 个真实 mod 的实战里，吞吐全部来自**跨文件并发**，单文件分片
   一次都没用上（20 个文件全部单连接成功）。
2. **成本是实打实的**：动态拆片要做进度聚合、兄弟分片取消、区间簿记与
   续传元数据合并，还要处理"拆出来的新片落到哪个 Range、和已有 .part
   区间怎么对齐"；机械盘上多段随机写还会放大寻道。
3. **首包成本被忽略不得**：每条新连接要先付一次连接建立 + TLS 握手
   （本机实测 ~0.69s，**与文件大小无关**），片切得越碎，这笔固定开销占比越高。

所以结论是：**按大小静态分级值得做（已做），动态拆片不值得做（不做）**。
真要优化单文件速度，正确的旋钮是 `large_file_multi_first_bytes` 与
`target_part_size`，而不是运行时拆片。

---

## 四、新增配置项

全部有默认值，**旧数据库不需要迁移**（`get_download_options()` 会与内置默认值合并）。
带 ✅ 的项会出现在「首选项 → 下载」里。

| 键 | 默认 | 作用 |
|---|---|---|
| ✅ `read_poll_interval` | `1.0` | socket 读超时轮询粒度（秒）；同时决定单次阻塞读的最长时间 |
| ✅ `part_retry` | `1` | 单个分片失败后的重试次数 |
| ✅ `http_status_check` | `1` | 非 2xx 视为失败、不落盘 |
| ✅ `part_meta_enabled` | `1` | 用 `.part.meta` 记录可信区间（安全续传） |
| ✅ `cleanup_residual_parts` | `1` | 下载结束清理缓存目录里残留的 `.part*` |
| `range_validate` | `1` | 分片必须 206 且 `Content-Range` 一致 |
| `part_meta_trust_size` | `0` | 无 sidecar 时也信任 `.part` 原始大小（旧行为，危险） |
| `shared_ssl_context` | `1` | 进程内共享 SSLContext |
| `hash_while_streaming` | `1` | 单连接下载边下边算哈希 |
| `fallocate` | `1` | 分片优先 `os.posix_fallocate` 预分配 |
| `keepalive_retry` | `1` | 陈旧 keep-alive 连接透明重试次数 |
| `release_response_timeout` | `2.0` | 排空响应体的最长等待（秒） |
| `backoff_max_ms` | `8000` | 指数退避上限（毫秒） |
| `backoff_jitter` | `0.3` | 退避抖动比例 |
| `retryable_status_retry` | `1` | 408/425/429/5xx 的额外重试次数 |
| `user_agent` | `PulsesEasier/1.0` | 请求 User-Agent |
| ✅ `disk_aware_slots` | `1` | 按目标盘类型限制并发（总开关） |
| ✅ `disk_type_override` | `""` | 留空=自动探测；可填 `ssd`/`hdd`/`network`/`removable`/`unknown` |
| ✅ `hdd_max_files` | `4` | 机械盘并发文件数上限 |
| ✅ `hdd_part_threads` | `1` | 机械盘单文件分片线程上限（1=不分片） |
| `ssd_max_files` / `ssd_part_threads` | `0` / `0` | 固态：0=不额外限制 |
| `network_max_files` / `network_part_threads` | `6` / `1` | 网络盘 / 网盘挂载 |
| `removable_max_files` / `removable_part_threads` | `2` / `1` | U 盘 / 移动硬盘 / 光驱 |
| `disk_probe_fallback` | `1` | Windows 下 IOCTL 失败时用 PowerShell 兜底 |
| `disk_probe_timeout` | `2.0` | 兜底探测超时（秒） |
| ✅ `multi_part_min_bytes` | `4 MiB` | 小于该大小永不分片 |
| ✅ `target_part_size` | `8 MiB` | 目标片大小，用来算初始分片数 |
| ✅ `max_part_count` | `8` | 初始分片数上限 |
| ✅ `large_file_multi_first_bytes` | `0` | >0 时超大文件先试分片（默认仍单连接优先） |

初版已有的 16 个下载配置键默认值**全部未变**
（`multi_slots` 在 `downloader._DEFAULTS` 是 12、在 `database.DEFAULT_DOWNLOAD_OPTIONS`
是 16，这是原项目既有的差异，本次未改动）。

---

## 五、实测对比（本地模拟源，成对 A/B，中位数）

原版 = 本次上传 ZIP 里的 `app/core/downloader.py` 逐字副本；
改进版 = 本次交付文件。计时场景给双方都把 `read_idle_timeout` 设成 3 秒
（对原版是**放宽**，它默认 20 秒），所以下表是真实收益的**下界**。

| 场景 | 原版 | 改进版 | 结论 |
|---|---|---|---|
| HTTP 12×24KB（8 槽位） | 6.072s | **0.076s** | **79.6×** |
| HTTP 3 个文件，双方出厂默认参数 | 61.57s（20.52s/文件） | **0.097s**（0.032s/文件） | **638×** |
| HTTPS 8×8KB（8 槽位） | 3.611s | **0.674s** | **5.4×**（含共享 SSLContext） |
| HTTPS 24×8KB（8 槽位） | 10.156s | **1.091s** | **9.3×** |
| 换源：10 个文件 `[404, 正常源]` | 成功 0/10 | **成功 10/10** | 换源真正生效 |
| 预分配零填充 `.part`（无哈希） | ❌ 零填充被判成功 | ✅ 重新下载、内容正确 | 数据损坏修复 |
| 未知大小（chunked） | ❌ 失败（12.1s） | ✅ 成功（0.0s） | 读空闲误判修复 |
| 8 MiB 单文件 | 3.09s，无残片 | **0.07s**，无残片、无中间分片 | 分片直写 + rename 收尾 |
| 12 个文件顺序下载 | 36.31s，连接 1 | **0.52s**，连接 1 | **70×**，keep-alive 复用正常 |

### 关于"拼接时间"

代码审查确认：**当前实现全程没有拼接路径**——分片直接 `seek` 写入同一个
`.part`，收尾是 `os.replace()`（本机实测 19.8µs）。旧式「N 个分片文件 +
最后拼起来」会产生 **3 倍** 磁盘 IO（写 + 读 + 再写），1 GB 文件仅拼接就要
约 5 秒串行时间；本次改动没有引入任何拼接，也不会回到那条路。
`*.part.[0-9]*` 中间文件在两个实现里都不产生（已实测确认为"无"）。

### 真实网络实战测试（Modrinth 10 + CurseForge 10，共 69.0 MB）

不只是本地模拟源。用项目**自己的**分类链路跑：

```
Modrinth 官方 API      → 10 个不同 mod（378KB ~ 18.3MB）
CurseForge（元数据经    → 10 个不同 mod（20KB ~ 12.9MB）
 mod.mcimirror.top 镜像，
 文件仍走 edge.forgecdn.net）
        ↓
differ.diff_packs_parallel()                     ← 项目自己的分类
        ↓ added=20 modified=0 deleted=0
PlayerView._collect_download_tasks_from_diff()   ← 项目自己的任务生成
        ↓ 20 个 DownloadTask
三轮对照：A 裸 urllib 顺序 / B 原版 downloader / C 新版 downloader
        ↓ 每个文件逐字节 SHA1 校验
```

第一回合（20 个文件 / 69.0 MB，真实网络）：

| 方式 | 耗时 | 平均吞吐 | SHA1 校验 | 说明 |
|---|---|---|---|---|
| **A** 裸 urllib 顺序（一个下完再下一个） | **567.91s** | 0.12 MB/s | **18/20** | 2 个文件因 120s 读超时**彻底失败**（无重试、无续传，只剩半截文件） |
| **B** 原版 downloader（出厂默认） | **156.22s** | 0.44 MB/s | 20/20 | 日志里仍出现「读空闲超过 20s」；靠"预分配 `.part` 大小达标即当成下完"这条**危险捷径**救回 |
| **C** 新版 downloader（本次交付） | **22.59s** | **3.05 MB/s** | 20/20 | 无一次停滞误判 |

**C/A = 25.14×   C/B = 6.91×**（同一批文件、同一时刻的链路）

> 这条链路本身很慢且抖动极大（单连接实测 84 KB/s ~ 1.3 MB/s，
> 不同时刻能差 15 倍）。所以上面这组数字**不是**纯粹的引擎差距，
> 但方向与量级都指向同一件事：**顺序单连接在这条链路上会被单个慢流拖死，
> 而并发 + 快速甩掉慢源 + 失败续传才是决定性的。**
> 随机网络抖动用下面的"轮转配对回合"单独量化。

> 另外注意 B 的 20/20 是靠那条危险捷径拿到的：只要 `.part` 的大小凑够
> `file_size`，它就认为文件已下完（**不校验内容**）。这正是 G4 修掉的缺陷——
> 在真实网络上它确实"救"回了文件，但也正是同一段代码会把预分配的零填充
> 文件判成下载成功。

**轮转配对回合**（最小的 8 个文件 / 4.0 MB，跑 3 轮，每轮把 A/B/C 的先后顺序
轮换，抵消网络随时间漂移；每臂开跑前先测一次「单连接取 256KB 的耗时」作为
当刻网络基准）：

| 方式 | 三轮耗时 | 中位 | 归一化（÷当刻网络基准）三轮 | 归一化中位 |
|---|---|---|---|---|
| A 裸 urllib 顺序 | 14.91 / 29.75 / 84.19 s | 29.75s | 10.97 / 30.61 / 129.05 | **30.61×** |
| B 原版 downloader | 26.72 / 47.46 / 64.00 s | 47.46s | 39.56 / 53.01 / 17.96 | **39.56×** |
| C 新版 downloader | 18.25 / 6.11 / 10.07 s | **10.07s** | 18.98 / 1.83 / 3.54 | **3.54×** |

* 原始中位：**C/A = 2.96×，C/B = 4.71×**
* 归一化中位：**C/A ≈ 8.6×，C/B ≈ 11.2×**

同一批文件、同一台机器，三轮轮换顺序后 C 始终最快；把网络本身的快慢除掉
以后差距更大，说明这不是"某一轮网络恰好快"的运气。

**实战结论**

| 观察 | 数据 |
|---|---|
| 全部文件真实可用性 | 20/20 SHA1 逐字节校验通过（C），B 也 20/20 但靠危险捷径，A 只有 18/20 |
| 顺序单连接是最大瓶颈 | 单连接实测 84 KB/s~1.3 MB/s，且会因单个流卡死而拖垮整个队列 |
| 并发是最主要的收益来源 | A→C 原始 25.1×、轮转中位 3.0×、归一化 8.6× |
| 原版的核心代价是"白等超时" | 20 个文件 × 20s 读空闲 ÷ 12 槽位 ≈ 40s 纯浪费；小文件回合里 B 的中位归一化 39.56×（几乎全是等待时间） |
| 失败处理决定成败 | A 丢掉 2 个文件（无重试无续传）；B/C 都靠续传/重试拿回全部 20 个 |
| 磁盘策略在本机未触发 | 目标盘 `/dev/sdb2` rotational=0 → SSD → 不额外限制（日志有记录） |

---

## 六、兼容性与行为变化

**没变**（`tests/test_basic.py::TestInit::test_public_api_shape` 逐项断言）：

* `download_files(tasks, target_dir, threads, progress, log, byte_progress, should_abort, on_file_started, on_file_done, on_file_failed)` 的参数名、顺序、默认值；
* `DownloadTask(rel_path, urls, sha1, sha256, sha512, file_size)` 与
  `DownloadResult(task, ok, target, error, aborted)` 的字段与顺序；
* `estimate_eta`、`invalidate_options` 的行为；
* 既有配置键的默认值；
* `player_view.py` 的调用方式（一个参数都不用改）。

**语义有变化（3 处，均为修正，调用方按预期受益）**：

1. **返回列表**：每个任务返回一条最终结果（成功 + 失败）。
   这与 `player_view` 的 `len(results) - len(failed)` 统计口径一致，
   修好了"成功数永远是 0"和 `_completed_files` 永远为空的问题。
2. **磁盘残留**：网络类失败会保留 `.part` + `.part.meta`，让同一次运行内的
   **换源续传**能接着下；`_attempt_file` 结束时仍会统一清理，
   所以不会跨任务留残留。`cleanup_residual_parts=0` 可以连收尾清理也关掉。
3. **进度回调**：`progress(done, total, name)` 的 `done` 只统计最终态
   （降级到单线程不再重复计数），因此**不会再超过 `total`**；
   `byte_progress` 改为单调聚合，不再回退。

---

## 七、测试

```bash
# 基础测试：编译 / 全模块导入 / 启动链路 / 配置一致性（20 项）
python3 tests/test_basic.py

# 功能测试：调度 / 降级换源 / 超时 / 分片分级 / 进度 / 中止 / 磁盘策略 / 耐心重试并行（51 项）
python3 tests/test_downloader.py

# 真实网络实战：Modrinth 10 + CurseForge 10（约 70MB，跑完自动清理）
python3 tests/realworld_test.py

# 与原版的成对 A/B 实测（约 3.5 分钟）
python3 tests/bench_ab.py
```

* **基础测试 32/32 通过**：`compileall` 全通过；26 个核心模块真实导入；
  31 个 UI 模块在桩依赖下导入；**用桩把 `main.py` 的启动链路整条跑通**
  （组类 → 构造主窗口 → `prepare_offscreen_warmup` 双端预热 →
  `get_boot_tasks` → 5 个 boot 任务全部执行）。注意：本机无 tkinter / 无显示器，
  **不覆盖真实 Tk 渲染**，Windows 10 上的界面仍需实机点一遍。
* **功能测试 51/51 通过**：本地模拟源覆盖正常源、404、503、忽略 Range、
  慢源、停滞源、截断响应、chunked、重定向、预分配 `.part`、断点续传、
  哈希（边下边算 / 不匹配拒绝）、进度单调、回调顺序、中止响应时间。
* 本轮新增 10 项：分片分级表、按配置夹取、区间连续性、小文件永不分片、
  大文件先手分片（可配）、默认仍单连接优先、启动探测只做一次、
  清缓存后可重探、耐心重试与主池并行、全失败仍返回全部任务。
* 磁盘策略 8 项：文件系统类型归类、本机探测与缓存、探测失败不干预、
  `disk_type_override` 五种取值、**只降不升**、机械盘策略下不进分片路径、
  固态策略下正常分片、策略写进日志。
* 逐项日志见 `tests/logs/`。

---

## 八、本次没做 / 后续建议

* **更新检查改用批量接口**（收益最大的一项，但属于上层逻辑）：
  Modrinth `POST /v2/version_files/update` 一次问清所有本地文件的更新版本
  （实测 7 个文件 1 次请求 0.87s，对比逐个 `GET /project/{slug}/version`
  9.16s / 4 个文件）；CurseForge `POST /v1/fingerprints` + `POST /v1/mods`。
* ~~按磁盘类型限制"并发文件数"~~ → **本轮已实现（G11）**。
* **在真机上验证 Windows 的磁盘探测**：`GetDriveTypeW` 与
  seek-penalty IOCTL 已在代码里按文档实现，但本机是 Linux，
  **Windows 分支没有实机验证过**。上机后看一眼日志里的
  `磁盘类型判定为 …` 是否符合实际；不对就用 `disk_type_override` 手工指定。
* **绝不逐块 `fsync`**：实测 3.15ms/次，等于把吞吐压到 ~31 MB/s；
  当前实现不做逐块 fsync，改动中也没有引入，后续也不要加。
* HTTP/2 多路复用（需要换掉 `http.client`，收益不确定，本轮未动）。

# Pulses Easier 更新日志

## v0.2.0 — 下载引擎增量改进（2026-10-05）

本次只改下载链路与它直接相关的配置，**不重写既有架构**：
两级槽位池（多线程槽位 → 单线程重试队列）、分片直写、快速筛除(B2) 全部保留。
所有对外接口（`download_files` 的参数、`DownloadTask` / `DownloadResult`
的字段、`estimate_eta` / `invalidate_options`）**一个都没动**，
`player_view.py` 无需改动调用方式。

---

## 一、需要替换的文件

| 文件 | 归属 | 说明 |
|---|---|---|
| `app/core/downloader.py` | 本次改动 | 下载引擎主体：5 个缺陷修复 + 5 项性能/健壮性改进 |
| `app/core/database.py` | 本次改动 | 新增配置项默认值与首选项说明（纯追加，旧库自动兼容） |
| `app/ui/widgets/preferences_dialog.py` | 本次改动 | 新配置项的类型登记（整数/浮点，各 1 行） |
| `app/core/__init__.py` | 本次改动 | 版本号 `0.1.0` → `0.2.0` |
| `app/ui/player_view.py` | 你方最新版 | 槽位面板「收起」回调桥接（`on_toggle`） |
| `app/ui/widgets/slot_panel.py` | 你方最新版 | 标题栏 + 收起按钮，回调 `on_toggle(False)` |

> 后两个是本次上传的最新版原样收录，方便一次性对齐；如果你那边已经是最新版，
> 可以只替换前四个。

文件校验（md5）：

```
c937460735479571b91142c9071dd129  app/core/downloader.py
07bfb3376f3bbd58bef1d947f2bbb26f  app/core/database.py
b6b98dd5372202b688b85edbab57b24c  app/core/__init__.py
6201564768d2337195bce7bbb5b0a92e  app/ui/widgets/preferences_dialog.py
dfe28178a5add1aabc01ca790d356971  app/ui/player_view.py
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
| HTTP 12×24KB（8 槽位） | 6.072s | **0.074s** | **81.8×** |
| HTTP 3 个文件，双方出厂默认参数 | 61.57s（20.52s/文件） | **0.095s**（0.032s/文件） | **651×** |
| HTTPS 8×8KB（8 槽位） | 4.097s | **1.060s** | **3.9×**（含共享 SSLContext） |
| HTTPS 24×8KB（8 槽位） | 10.158s | **1.064s** | **9.6×** |
| 换源：10 个文件 `[404, 正常源]` | 成功 0/10 | **成功 10/10** | 换源真正生效 |
| 预分配零填充 `.part`（无哈希） | ❌ 零填充被判成功 | ✅ 重新下载、内容正确 | 数据损坏修复 |
| 未知大小（chunked） | ❌ 失败（12.1s） | ✅ 成功（0.0s） | 读空闲误判修复 |
| 8 MiB 单文件 | 3.09s，无残片 | **0.07s**，无残片、无中间分片 | 分片直写 + rename 收尾 |
| 12 个文件顺序下载 | 36.31s，连接 1 | **0.50s**，连接 1 | **73×**，keep-alive 复用正常 |

### 关于"拼接时间"

代码审查确认：**当前实现全程没有拼接路径**——分片直接 `seek` 写入同一个
`.part`，收尾是 `os.replace()`（本机实测 19.8µs）。旧式「N 个分片文件 +
最后拼起来」会产生 **3 倍** 磁盘 IO（写 + 读 + 再写），1 GB 文件仅拼接就要
约 5 秒串行时间；本次改动没有引入任何拼接，也不会回到那条路。
`*.part.[0-9]*` 中间文件在两个实现里都不产生（已实测确认为"无"）。

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

# 功能测试：初始化 / 多线程调度 / 降级换源 / 超时 / 分片 / 进度 / 中止（32 项）
python3 tests/test_downloader.py

# 与原版的成对 A/B 实测（约 3.5 分钟）
python3 tests/bench_ab.py
```

* **基础测试 20/20 通过**：`compileall` 全通过；26 个核心模块真实导入；
  31 个 UI 模块在桩依赖下导入；**用桩把 `main.py` 的启动链路整条跑通**
  （组类 → 构造主窗口 → `prepare_offscreen_warmup` 双端预热 →
  `get_boot_tasks` → 4 个 boot 任务全部执行）。注意：本机无 tkinter / 无显示器，
  **不覆盖真实 Tk 渲染**，Windows 10 上的界面仍需实机点一遍。
* **功能测试 32/32 通过**：本地模拟源覆盖正常源、404、503、忽略 Range、
  慢源、停滞源、截断响应、chunked、重定向、预分配 `.part`、断点续传、
  哈希（边下边算 / 不匹配拒绝）、进度单调、回调顺序、中止响应时间。
* 逐项日志见 `tests/logs/`。

---

## 八、本次没做 / 后续建议

* **更新检查改用批量接口**（收益最大的一项，但属于上层逻辑）：
  Modrinth `POST /v2/version_files/update` 一次问清所有本地文件的更新版本
  （实测 7 个文件 1 次请求 0.87s，对比逐个 `GET /project/{slug}/version`
  9.16s / 4 个文件）；CurseForge `POST /v1/fingerprints` + `POST /v1/mods`。
* **按磁盘类型限制"并发文件数"**：本机 SSD 上并发文件数从 12 升到 24 时写入
  吞吐反而掉到 ~70%（87→106→74 MB/s），机械盘会放大数倍。可按
  `/sys/block/*/queue/rotational`（Windows 用盘符类型）把 HDD 的
  `multi_slots` 上限压到 4。
* **绝不逐块 `fsync`**：实测 3.15ms/次，等于把吞吐压到 ~31 MB/s；
  当前实现不做逐块 fsync，改动中也没有引入，后续也不要加。
* HTTP/2 多路复用（需要换掉 `http.client`，收益不确定，本轮未动）。

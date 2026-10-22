"""
aster.py — Aster 引擎（自适应分片下载）
========================================================================
名字取自拉丁语「星」，中文别称 **星驰**（与 RideX 锐驰 对应）。

策略移植自 Plain Craft Launcher 2 的 NetTask（ModNet.vb），但不共享任何
代码：Aster 有自己的调度器、分片模型和失败判定，和 RideX 完全独立。
和 RideX 完全独立：不共用任何调度/分片代码，只共用任务与结果的数据结构
（`DownloadTask` / `DownloadResult`）以及最终的哈希校验函数 `_verify`。

为什么单独做一条：两条引擎的取舍不一样，留着可以按场景切、各自演进。

移植过来的策略（对应 PCL 源码 ModNet.vb 里的常量与分支）：

  1. **单线程起步、自适应增开**
     PCL 不预先切成 N 片，而是先开 1 个线程，然后盯着"剩余量最大的那一块"，
     只要它还剩 > 256 KB 就从它的**尾部切 40%** 出来开新线程
     （`FilePieceLimit = 256 KB`、`StartPosition = End - Undone * 0.4`）。
     这样慢文件自然只跑 1~2 个线程，快文件能迅速铺满带宽。

  2. **全局线程预算**
     `NetTaskThreadLimit`（设置项 + 1）是所有文件共享的上限，不是每文件上限。
     本实现对应 `aster_threads`。

  3. **不分割的情况**
     - 文件大小未知（没有 Content-Length）
     - 文件 < 1 MB
     - 下载源属于"多线程容易抽风"的名单（bmclapi / github / optifine /
       meloong / pcl2-server / momot.rs）
     这三种都退化成单线程整文件下载。

  4. **下载源逐个降级**
     每个源记 `FailCount`。出现 404 / 502 / 416 / 范围不匹配 / 合并校验失败，
     或连续失败过多时把该源标记为不可用，换下一个源接着下**同一个位置**。
     BMCLAPI 的 403/429 不算致命（PCL 里专门豁免）。

  5. **全源失败 → 整文件单线程重试一次**
     PCL 的 `Retried` 分支：把所有源重置成"只允许单线程"，从 0 开始重来一遍。
     这一步专治"多线程下会抽风、单线程反而正常"的源。

  6. **慢速断链**
     连续 5 秒没有新数据且速度 < 1 KB/s → 主动断开换源（`TimeoutException`）。
     连接超时按 `clamp(平均连接耗时, 15s, 30s) * (1 + FailCount)` 递增，上限 30s。

与 PCL 的差异（实现手段，不影响策略）：
  - PCL 把每个线程写进独立临时文件再合并；这里直接按**绝对偏移**写进同一个
    文件（预分配大小）。效果等价，且省掉一次全文件拷贝。
  - PCL 用 C# 的 HttpClient 连接池；这里用 urllib，每次请求独立连接。
  - 断点续传的粒度是"块"：某块失败后保留已写字节，换源从断点继续。
"""

import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import mirrors
from ..downloader import DownloadResult, DownloadTask, _verify

# ---- 移植过来的常量（对应 ModNet.vb）------------------------------------
PIECE_LIMIT = 256 * 1024          # FilePieceLimit：剩余量超过它才值得再开一个线程
TAIL_RATIO = 0.4                  # 从最大碎片尾部切走的比例
MIN_SPLIT_SIZE = 1024 * 1024      # < 1 MB 不分割
NO_SPLIT_HOSTS = (                # PCL 里明确禁用多线程的源
    "pcl2-server", "meloong.com", "bmclapi",
    "github.com", "optifine.net", "momot.rs",
)
CONNECT_TIMEOUT = 15.0
MAX_TIMEOUT = 30.0
SLOW_GAP_MS = 5000                # 连续这么久没新数据 → 判定卡死
SLOW_BYTES = 1024                 # 且这段时间只收到不到 1 KB
READ_BLOCK = 16 * 1024
DEFAULT_BUDGET = 32               # 全局线程预算（设置项 aster_threads）
TICK = 0.02                       # 调度轮询间隔

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _options() -> dict:
    """读下载配置（优先用下载器那份缓存，测试注入也走这里）。"""
    try:
        from ..downloader import _get_options
        return dict(_get_options())
    except Exception:  # noqa: BLE001
        try:
            from .. import database as db
            return dict(db.get_download_options())
        except Exception:  # noqa: BLE001
            return {}


def _timeouts(threads: int) -> tuple[float, float]:
    """
    读设置里的超时参数，返回 (socket 超时, 无数据判定间隔)。

    单连接（耐心档 / threads<=1）用 single_* 那组，多线程用普通那组；
    读不到就用 PCL 的默认值。
    """
    o = _options()

    def num(key: str, default: float) -> float:
        try:
            return float(o.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    if threads and threads <= 1:
        gap = num("single_stall_timeout", 30)
    else:
        gap = num("stall_timeout", 10)
    gap = max(1.0, min(gap, 120.0))
    return max(3.0, gap), gap

_SSL = ssl.create_default_context()


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


# ----------------------------------------------------------------------
# 下载源
# ----------------------------------------------------------------------
class _Source:
    __slots__ = ("url", "fail_count", "failed", "single_only")

    def __init__(self, url: str):
        self.url = url
        self.fail_count = 0
        self.failed = False
        self.single_only = False      # 是否只允许单线程（PCL 的 SourcesOnce）


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def _no_split_url(url: str) -> bool:
    host = _host_of(url)
    return any(h in host for h in NO_SPLIT_HOSTS)


# ----------------------------------------------------------------------
# 错误分类
# ----------------------------------------------------------------------
class _RangeError(Exception):
    """下载源不支持分段（或返回的分段不对）→ 该源不能用于多线程。"""


class _SourceError(Exception):
    """该源这次不可用（404/502/…）。"""

    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


# ----------------------------------------------------------------------
# 分块
# ----------------------------------------------------------------------
class _Chunk:
    """文件里的一段连续区间。pos 是"下一个要写的绝对偏移"。"""

    __slots__ = ("start", "pos", "end", "source", "running", "fail_count")

    def __init__(self, start: int, end: int, source: _Source | None = None):
        self.start = start
        self.pos = start
        self.end = end
        self.source = source
        self.running = False
        self.fail_count = 0

    @property
    def done(self) -> int:
        return max(0, self.pos - self.start)

    @property
    def undone(self) -> int:
        return max(0, self.end - self.pos + 1)


# ----------------------------------------------------------------------
# 单个文件的任务
# ----------------------------------------------------------------------
class _Job:
    def __init__(self, task: DownloadTask, target: Path):
        self.task = task
        self.target = target
        # 先写临时名，校验通过才 rename 到正式名。
        # 和 RideX 的 .part 一样：中途被杀不会留下"看起来已下完"的残文件。
        self.temp = target.with_name(target.name + ".aster-tmp")
        self.size = -2                  # -2 未获取；-1 未知大小；>=0 已知
        self.chunks: list[_Chunk] = []
        # 可重入：_next_spec 会在持锁时调 pick_source（同一个锁）
        self.lock = threading.RLock()
        # 镜像优先：Modrinth 官方 CDN 在国内偏慢，先试镜像，挂了自动退回
        self.sources = [_Source(u) for u in mirrors.ordered(list(task.urls or []))]
        self.once: list[_Source] = []   # 只允许单线程的源
        self.retried = False
        self.finished = False
        self.failed_reason = ""
        self.aborted = False
        self.connect_ms = 0
        self.connect_count = 0
        self.last_report = 0
        self.fh = None                  # 预分配好的目标文件句柄
        self.file_lock = threading.Lock()
        self.last_source = 0
        self.last_error = ""            # 最后一个源错误（失败原因里带上）
        self.sock_timeout = CONNECT_TIMEOUT
        self.gap_ms = SLOW_GAP_MS
        self.retry_limit = 2          # 同一个源"毫无进展"最多试几次

    # -- 大小 / 分片状态 -------------------------------------------------
    @property
    def no_split(self) -> bool:
        if self.retried:
            return True          # 整文件重试：只允许单线程
        if self.size < 0:
            return True
        if self.size < MIN_SPLIT_SIZE:
            return True
        return all(_no_split_url(s.url) for s in self.sources) \
            if self.sources else True

    def reset(self, size: int = -2):
        with self.lock:
            self.chunks = []
            self.size = size
            self.retried = True

    def total_done(self) -> int:
        with self.lock:
            return sum(c.done for c in self.chunks)

    def pending(self) -> list[_Chunk]:
        with self.lock:
            return [c for c in self.chunks if not c.running and c.undone > 0]

    def all_done(self) -> bool:
        with self.lock:
            if not self.chunks:
                return False
            if any(c.running for c in self.chunks):
                return False
            if self.size < 0:
                # 未知大小：单线程读到 EOF 就算完
                return all(c.undone <= 0 for c in self.chunks)
            if self.size == 0:
                return True
            return all(c.undone <= 0 for c in self.chunks) and \
                sum(c.done for c in self.chunks) >= self.size

    # -- 选源 -------------------------------------------------------------
    def pick_source(self) -> _Source | None:
        """按轮转顺序取一个还能用的源。"""
        with self.lock:
            live = [s for s in self.sources if not s.failed]
            if live:
                s = live[self.last_source % len(live)]
                self.last_source = (self.last_source + 1) % len(live)
                return s
            if self.once:
                return self.once[0]
            return None

    def mark_source_failed(self, src: _Source, fatal: bool, done: int,
                           budget: int) -> bool:
        """
        按 PCL 的规则判断要不要禁用这个源。返回是否真的禁用了。
        """
        with self.lock:
            src.fail_count += 1
            # 同一源连续失败、且一个字节都没拿到 → 别死磕（默认 2 次）
            limit = min(min(max(int(budget), 5), 30),
                        max(1, int(getattr(self, "retry_limit", 2))))
            should = (
                fatal
                or (src.fail_count >= limit and done < 1)
                or src.fail_count > budget + 2
            )
            if not should:
                return False
            if src.failed:
                return False
            src.failed = True
            if src in self.once:
                self.once.remove(src)
            return True

    def has_source(self) -> bool:
        with self.lock:
            return any(not s.failed for s in self.sources) or bool(self.once)

    # -- 文件句柄 ---------------------------------------------------------
    def open_file(self):
        with self.file_lock:
            if self.fh is not None:
                return self.fh
            self.target.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.temp.exists()
            fh = open(self.temp, "r+b" if not fresh else "w+b")
            if self.size and self.size > 0 and fresh:
                fh.truncate(self.size)
            self.fh = fh
            return fh

    def close_file(self):
        with self.file_lock:
            if self.fh is not None:
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = None

    def write_at(self, offset: int, data: bytes) -> bool:
        with self.file_lock:
            fh = self.fh
            if fh is None:
                return False
            fh.seek(offset)
            fh.write(data)
            return True


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def _open(url: str, start: int, timeout: float):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "identity",
    })
    if start > 0:
        req.add_header("Range", f"bytes={start}-")
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)


def _content_length(resp) -> int:
    raw = resp.headers.get("Content-Length")
    try:
        return int(raw) if raw is not None else -1
    except (TypeError, ValueError):
        return -1


def _range_start(resp) -> int:
    raw = resp.headers.get("Content-Range", "")
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", raw.strip())
    return int(m.group(1)) if m else -1


# ----------------------------------------------------------------------
# 线程体
# ----------------------------------------------------------------------
def _run_chunk(job: _Job, chunk: _Chunk, src: _Source, budget: int,
               byte_cb, should_abort) -> None:
    """下载一个分块。出错时按 PCL 规则处理源并把这个块放回队列。"""
    timeout = min(max(job.sock_timeout, job.gap_ms / 1000.0)
                  * (1 + src.fail_count), MAX_TIMEOUT)
    start = chunk.pos
    resp = None
    try:
        t0 = _now_ms()
        resp = _open(src.url, start, timeout)
        status = getattr(resp, "status", 200)
        length = _content_length(resp)

        if start == 0:
            if job.size == -2:
                job.size = length if length >= 0 else -1
            job.open_file()
            with job.lock:
                if chunk.end < 0 and job.size >= 0:
                    chunk.end = job.size - 1
        else:
            if status != 206:
                raise _RangeError(f"源不支持 Range（返回 {status}）")
            got = _range_start(resp)
            if got >= 0 and got != start:
                raise _RangeError(f"Range 起点不符（要 {start}，给 {got}）")
            if job.size >= 0 and length >= 0:
                expect = job.size - start
                if length != expect:
                    raise _RangeError(
                        f"分段长度不符（要 {expect}，给 {length}）")

        job.open_file()          # 幂等：分片线程也必须先拿到句柄

        job.connect_ms = int((job.connect_ms * job.connect_count
                              + (_now_ms() - t0)) /
                             (job.connect_count + 1))
        job.connect_count += 1

        last_recv = -1
        while True:
            if should_abort is not None and should_abort():
                job.aborted = True
                with job.lock:
                    chunk.running = False
                return
            if chunk.end >= 0 and chunk.pos > chunk.end:
                break
            try:
                block = resp.read(READ_BLOCK)
            except Exception as e:  # noqa: BLE001
                raise _SourceError(f"读取失败：{e}") from e
            if not block:
                break
            if chunk.end >= 0:
                room = chunk.end - chunk.pos + 1
                if len(block) > room:
                    block = block[:room]
            job.write_at(chunk.pos, block)
            if last_recv < 0:
                mirrors.note_success(src.url)
            with job.lock:
                chunk.pos += len(block)
                src.fail_count = 0
            if byte_cb:
                byte_cb(job.task.rel_path, job.total_done(),
                        job.size if job.size > 0 else -1)
            last_recv = _now_ms()
            if chunk.end >= 0 and chunk.pos > chunk.end:
                break
            if _now_ms() - last_recv > job.gap_ms * 3:
                raise _SourceError("超时（长时间没有数据）")
        # 正常结束
        with job.lock:
            chunk.running = False
            if chunk.end < 0 and job.size >= 0:
                chunk.end = job.size - 1
            incomplete = chunk.end >= 0 and chunk.pos <= chunk.end
            no_progress = chunk.pos == start
        if incomplete and no_progress:
            _fail(job, chunk, src, "没有收到任何数据", fatal=False,
                  budget=budget)
    except _RangeError as e:
        _fail(job, chunk, src, str(e), fatal=True, budget=budget)
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        fatal = code in (404, 502) or (code in (403, 429)
                                       and "bmclapi" not in src.url)
        if code == 416:
            fatal = True
        _fail(job, chunk, src, f"HTTP {code}", fatal=fatal, budget=budget)
    except urllib.error.URLError as e:
        _fail(job, chunk, src, f"连接失败：{e.reason}", fatal=False,
              budget=budget)
    except TimeoutError:
        _fail(job, chunk, src, "超时", fatal=False, budget=budget)
    except _SourceError as e:
        _fail(job, chunk, src, str(e), fatal=e.fatal, budget=budget)
    except Exception as e:  # noqa: BLE001
        _fail(job, chunk, src, f"{type(e).__name__}: {e}", fatal=False,
              budget=budget)
    finally:
        try:
            if resp is not None:
                resp.close()
        except Exception:  # noqa: BLE001, S110
            pass


def _fail(job: _Job, chunk: _Chunk, src: _Source, msg: str, fatal: bool,
          budget: int) -> None:
    mirrors.note_failure(src.url)
    if msg:
        job.last_error = msg
    with job.lock:
        chunk.running = False
        chunk.fail_count += 1
    job.mark_source_failed(src, fatal, chunk.done, budget)


# ----------------------------------------------------------------------
# 调度
# ----------------------------------------------------------------------
def _next_spec(job: _Job, budget: int):
    """
    给这个文件挑下一个要开的线程：(chunk, source) 或 None。
    对应 PCL 的 TryBeginThread。
    """
    if job.finished or job.aborted:
        return None
    with job.lock:
        if job.size < 0:
            # -2 = 还没探到大小；-1 = 源没给 Content-Length。
            # 两种情况都只能单线程整文件；块必须在调度器里登记，
            # 否则线程还没跑起来时会被重复派发 → 两个线程都从 0 写。
            if not job.chunks:
                src = job.pick_source()
                if src is None:
                    return None
                c = _Chunk(0, -1, src)
                c.running = True
                job.chunks.append(c)
                return c, src
            c = job.chunks[0]
            if c.running:
                return None
            src = job.pick_source()
            if src is None:
                return None
            c.source = src
            c.running = True
            return c, src
        if job.size == 0:
            return None

        if not job.chunks:
            return None
        if job.no_split:
            if len(job.chunks) > 1:
                return None
            if job.chunks[0].running:
                return None
            src = job.pick_source()
            if src is None:
                return None
            c = job.chunks[0]
            c.source = src
            c.running = True
            return c, src

        # 先补"没人管"的块（上次失败留下的断点）
        idle = [c for c in job.chunks if not c.running and c.undone > 0]
        if idle:
            c = max(idle, key=lambda x: x.undone)
            src = job.pick_source()
            if src is None:
                return None
            c.source = src
            c.running = True
            return c, src

        # 自适应增开：盯住剩余量最大的那块（**跑着的也算**，
        # PCL 就是这么干的：从它尾部切 40% 出来另开一线程）
        big = max(job.chunks, key=lambda c: c.undone)
        if big.undone < PIECE_LIMIT:
            return None
        cut = big.end - int(big.undone * TAIL_RATIO)
        if cut <= big.pos:
            return None
        src = job.pick_source()
        if src is None:
            return None
        new = _Chunk(cut, big.end, src)
        new.running = True
        big.end = cut - 1
        job.chunks.append(new)
        job.chunks.sort(key=lambda c: c.start)
        return new, src


def _finalize(job: _Job, log) -> None:
    """收尾：关文件 → 校验 → 失败则整文件单线程重试一次。"""
    job.close_file()
    ok, err = _verify(job.temp, job.task)
    if ok:
        try:
            os.replace(job.temp, job.target)
        except OSError as e:
            job.finished = True
            job.failed_reason = f"落盘失败：{e}"
            return
        job.finished = True
        return
    if job.retried:
        job.finished = True
        job.failed_reason = err or "下载失败"
        return
    # PCL 的 Retried 分支：所有源重置为"只允许单线程"，整文件重来
    if log:
        try:
            log("warn", f"{job.task.rel_path}：校验未通过（{err}），"
                        f"改用单线程整文件重试")
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        if job.temp.exists():
            job.temp.unlink()
    except OSError:
        pass
    with job.lock:
        job.once = list(job.sources)      # 全部降级成"只允许单线程"
        for s in job.sources:
            s.failed = True
            s.single_only = True
    job.reset()


def _cleanup_target(job: _Job):
    for p in (job.temp, job.target):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------
# 对外入口（与 RideX 同签名）
# ----------------------------------------------------------------------
def download_files(
    tasks: list[DownloadTask],
    target_dir: Path,
    threads: int | None = None,
    progress=None,
    log=None,
    byte_progress=None,
    should_abort=None,
    on_file_started=None,
    on_file_done=None,
    on_file_failed=None,
) -> list[DownloadResult]:
    """
    用 PCL 式调度下载一批文件。

    回调约定与 RideX 完全一致（player_view 两边都能直接用）。
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    budget = int(threads) if threads and threads > 0 else DEFAULT_BUDGET

    sock_timeout, gap_ms = _timeouts(budget)
    jobs = [_Job(t, target_dir / t.rel_path) for t in tasks]
    try:
        _retry = max(1, int(_options().get("retry_same_url", 1)) + 1)
    except Exception:  # noqa: BLE001
        _retry = 2
    for _j in jobs:
        _j.sock_timeout = sock_timeout
        _j.gap_ms = int(gap_ms * 1000)
        _j.retry_limit = _retry
    results: dict[int, DownloadResult] = {}
    total = len(jobs)
    finished_n = {"n": 0}
    lock = threading.Lock()
    last_byte = {"t": 0.0}

    def _report(done_files: int):
        if not progress:
            return
        try:
            progress(done_files, total, "")
        except Exception:  # noqa: BLE001, S110
            pass

    def _bytes(rel: str, received: int, total_bytes: int):
        if not byte_progress:
            return
        now = time.monotonic()
        if now - last_byte["t"] < 0.1:
            return
        last_byte["t"] = now
        try:
            byte_progress(rel, received, total_bytes)
        except Exception:  # noqa: BLE001, S110
            pass

    if total == 0:
        _report(0)
        return []

    _report(0)
    running: dict[int, int] = {}      # id(job) -> 活跃线程数
    active = {"n": 0}
    sched_lock = threading.Lock()

    def _spawn(job: _Job, chunk: _Chunk, src: _Source):
        def _body():
            try:
                _run_chunk(job, chunk, src, budget, _bytes, should_abort)
            finally:
                with sched_lock:
                    active["n"] -= 1
                    running[id(job)] = running.get(id(job), 1) - 1

        with sched_lock:
            active["n"] += 1
            running[id(job)] = running.get(id(job), 0) + 1
        t = threading.Thread(target=_body, daemon=True,
                             name=f"aster-{job.task.rel_path}")
        t.start()

    def _start_file(job: _Job):
        if on_file_started:
            try:
                on_file_started(job.task)
            except Exception:  # noqa: BLE001, S110
                pass

    def _finish(job: _Job, ok: bool, err: str = "", aborted: bool = False):
        idx = jobs.index(job)
        if idx in results:
            return
        res = DownloadResult(task=job.task,
                             ok=ok,
                             target=job.target if ok else None,
                             error=err,
                             aborted=aborted)
        results[idx] = res
        if ok:
            if on_file_done:
                try:
                    on_file_done(job.task)
                except Exception:  # noqa: BLE001, S110
                    pass
        else:
            if on_file_failed:
                try:
                    on_file_failed(job.task, err or "下载失败")
                except Exception:  # noqa: BLE001, S110
                    pass
        with lock:
            finished_n["n"] += 1
            _report(finished_n["n"])

    # 主调度循环：所有文件共享 budget 个线程
    started: set[int] = set()
    idle_rounds = 0
    while True:
        if should_abort is not None and should_abort():
            for job in jobs:
                if id(job) not in results:
                    _finish(job, False, "已中止", aborted=True)
            break
        progressed = False
        for job in jobs:
            if id(job) in results:
                continue
            if id(job) not in started:
                started.add(id(job))
                _start_file(job)
            # 全部块下完 → 收尾（校验 / 重试 / 完成）
            if job.size != -2 and job.all_done() and \
                    running.get(id(job), 0) == 0:
                if job.finished:
                    _finish(job, True)
                    continue
                _finalize(job, log)
                if job.finished:
                    if job.failed_reason:
                        _cleanup_target(job)
                        _finish(job, False, job.failed_reason)
                    else:
                        _finish(job, True)
                continue
            if job.size == 0:
                job.open_file()
                job.close_file()
                _finalize(job, log)
                if job.finished:
                    _finish(job, not job.failed_reason, job.failed_reason)
                continue
            # 没有可用源且没有在跑的线程 → 这个文件失败（连大小都没探到也算）
            if not job.has_source() and running.get(id(job), 0) == 0:
                if not job.failed_reason:
                    job.failed_reason = (
                        "所有下载源都失败了（%s）" % job.last_error
                        if job.last_error else "所有下载源都失败了")
                _cleanup_target(job)
                _finish(job, False, job.failed_reason)
                continue
            while True:
                with sched_lock:
                    if active["n"] >= budget:
                        break
                spec = _next_spec(job, budget)
                if spec is None:
                    break
                chunk, src = spec
                if chunk is not None:
                    _spawn(job, chunk, src)
                    progressed = True
        if len(results) >= total:
            break
        if not progressed and active["n"] == 0:
            idle_rounds += 1
            if idle_rounds > 250:          # 约 5 秒完全没动静
                for job in jobs:
                    if id(job) not in results:
                        _cleanup_target(job)
                        _finish(job, False, "下载停滞（没有可用线程）")
                break
        else:
            idle_rounds = 0
        if not progressed:
            time.sleep(TICK)

    for job in jobs:
        job.close_file()
    return [results[i] for i in sorted(results)]

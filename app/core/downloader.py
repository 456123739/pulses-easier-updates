"""
downloader.py — 两级槽位池下载器
------------------------------------------------
结构：
  [多线程槽位池] multi_slots 个 worker，每个跑一个文件
       ↓ 慢速/失败 → 移出
  [单线程重试队列] retry_queue
       ↓ 多线程池全部结束
  [单线程槽位池] single_slots 个 worker 顺序重试

A 分片直写：
  - 分片直接写入 target.part 的对应偏移（seek + write）
  - 不产生 .part.N 中间文件，无需拼接

B1 调度顺序：
  - 默认单连接（失败率低）
  - 单连接失败 2 次后才尝试分片
  - 分片失败直接进重试队列

B2 快速筛除（多线程槽位专用）：
  - 连续 3 秒速度 < 20 KB/s（含 0）→ 立即移出多线程槽位
  - 移出后不再尝试同文件其它 URL，直接进单线程重试队列
  - 单线程重试阶段用放宽参数（1 KB/s），不会反复被踢

A1 连接复用 / D 中止响应 / E 残留清理 / F 进度回调

--- 本轮增量改进（配置项见 _DEFAULTS，全部有默认值，旧库无需迁移）---

G1 多源回退修复：
  - 原实现里 single_fail_count 在 URL 循环外初始化，第 1 个 URL 用尽配额后
    直接 break，task.urls 的第 2 个及以后**永远不会被尝试**（换源失效）。
  - 现在每个候选 URL 都独立跑「单连接 N 次 → 分片一次」，全部失败才判失败。
  - retry_same_url 从死配置接回：每 URL 单连接尝试 retry_same_url+1 次。

G2 HTTP 状态码检查：
  - 原实现不检查 resp.status，404/403 的错误页会被当成文件写盘。
  - 现在非 2xx 抛 _HttpStatusError（区分 retry / dead），只读 ≤512B 摘要后丢弃连接。

G3 Range 校验：
  - 分片请求必须 status==206 且 Content-Range 与请求区间一致；否则判该源
    不支持分段（_RangeUnsupportedError）→ 换源重试，不产出损坏文件。

G4 .part 续传可信化：
  - 原实现 `existing_size >= file_size` 就直接 part.replace(target)，而分片预分配
    的 .part 大小恰好等于 file_size → **零填充的假文件被判为下载成功**。
  - 现在用 <target>.part.meta 记录**已确认完成的字节区间**，只信任连续前缀；
    没有 meta 的旧 .part 一律重新下载。part_meta_enabled=0 可退回旧行为。

G5 读空闲误判修复：
  - 原实现用 select() 探测原始 socket，而 http.client 的 BufferedReader 已把
    响应体预读进用户态 → select 看不到数据，把"已下完/正在下"误判为 stalled。
  - 现在删除 select，改为 socket 超时轮询 + HTTPResponse.read1()，
    每次超时回到循环顶部重新评估停滞/时限/速度窗口。
  - 副作用：未知大小（chunked / 无 Content-Length）也能正常下完。

G6 中止/停滞不再无限排空：
  - 原实现 finally 里无条件 resp.read()，中止后仍要把服务器剩余的响应体读完。
  - 现在改为 _release_response：读完才复用连接，没读完直接断开。

G7 共享 ssl.SSLContext：
  - 原实现每次 HTTPSConnection() 都新建 SSLContext 并重载 CA 信任库
    （实测约 48ms/连接，多线程下被 GIL 串行化）。
  - 现在进程内共享一个 context（shared_ssl_context，默认开）。

G8 分片进度聚合：
  - 原实现 byte_cb 上报的是**单片**字节数（0→片大小循环），分片并发时进度回退。
  - 现在 _FileProgress 聚合「已完成 + 各在途分片」，对外单调不回落。

G9 其他：陈旧 keep-alive 连接透明重试一次；指数退避 + 抖动；分片用
  os.posix_fallocate 预分配（不可用时回落 truncate）；单连接下载边下边算哈希
  （hash_while_streaming，省掉收尾的一次全文件重读）。

G10 返回语义对齐调用方：
  - 原实现 results 里只塞"非 ok"的结果，而 player_view 按
    `成功 = len(results) - len(failed)` 统计 → 成功数永远是 0；
    `if r.ok: self._completed_files.append(...)` 也永远收集不到东西。
  - 降级救回的文件还会在列表里留一条失败记录。
  - 现在：每个任务返回**恰好一条**最终结果（成功 + 失败），
    进度只在最终态推进（done 不会超过 total），
    on_file_failed 只在最终失败时触发。
"""

import hashlib
import http.client
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from urllib import error as urlerr

from . import database as db

_DEFAULTS = {
    "multi_slots": 12,
    "single_slots": 4,
    "defer_to_single_after": 0,
    "part_threads": 4,
    "max_connections": 128,
    "connect_timeout": 10,
    "read_idle_timeout": 20,
    "per_url_timeout": 90,
    "stall_timeout": 10,
    "min_speed_bps": 10240,
    "speed_window": 5.0,
    "retry_same_url": 1,
    "retry_backoff_ms": 500,
    "single_url_timeout": 180,
    "single_stall_timeout": 30,
    "single_min_speed_bps": 1024,
    # ---- 本轮新增（默认值 = 保持/修复既有行为） ----
    "read_poll_interval": 1.0,      # socket 读超时轮询粒度（秒，0=不设超时）
    "http_status_check": 1,         # 1=非 2xx 视为失败，不落盘
    "range_validate": 1,            # 1=分片必须 206 且 Content-Range 一致
    "part_meta_enabled": 1,         # 1=用 .part.meta 记录可信区间（续传）
    "part_meta_trust_size": 0,      # 1=没有 meta 时也信任 .part 的原始大小（旧行为，危险）
    "part_retry": 1,                # 单个分片失败后的重试次数
    "shared_ssl_context": 1,        # 1=进程内共享 SSLContext
    "hash_while_streaming": 1,      # 1=单连接下载边下边算哈希
    "fallocate": 1,                 # 1=分片预分配优先 os.posix_fallocate
    "keepalive_retry": 1,           # 陈旧 keep-alive 连接透明重试次数
    "release_response_timeout": 2.0,  # 排空响应体的最长等待（秒）
    "backoff_max_ms": 8000,         # 指数退避上限
    "backoff_jitter": 0.3,          # 退避抖动比例
    "retryable_status_retry": 1,    # 408/429/5xx 额外重试次数
    "cleanup_residual_parts": 1,    # 1=下载结束时清理 target_dir 下残留 .part*
    "user_agent": "PulsesEasier/1.0",
}

CHUNK_SIZE = 256 * 1024
BYTE_THROTTLE_MS = 100
PROGRESS_THROTTLE = 1

MULTI_PART_THRESHOLD = 4 * 1024 * 1024
MULTI_PART_COUNT = 4
MIN_PART_SIZE = 512 * 1024
SINGLE_FAIL_BEFORE_MULTI = 2

# 快速筛除阈值：多线程槽位里，连续 3s 平均速度 < 20KB/s → 立即移出
FAST_STALL_WINDOW = 3.0
FAST_STALL_MIN_BPS = 20 * 1024

_REDIRECT_LIMIT = 3
_ERROR_BODY_SNIPPET = 512
_RETRYABLE_STATUS = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

_CONN_SEM: threading.Semaphore | None = None
_CONN_SEM_SIZE = 0
_CONN_SEM_LOCK = threading.Lock()

_OPT_LOCK = threading.Lock()
_OPT_CACHE: dict | None = None


def _get_options() -> dict:
    global _OPT_CACHE
    with _OPT_LOCK:
        if _OPT_CACHE is not None:
            return _OPT_CACHE
        try:
            opts = db.get_download_options()
        except Exception:  # noqa: BLE001
            opts = {}
        merged = dict(_DEFAULTS)
        merged.update(opts)
        _OPT_CACHE = merged
        return _OPT_CACHE


def invalidate_options():
    global _OPT_CACHE
    with _OPT_LOCK:
        _OPT_CACHE = None


def _get_sem(max_conn: int) -> threading.Semaphore:
    global _CONN_SEM, _CONN_SEM_SIZE
    with _CONN_SEM_LOCK:
        if _CONN_SEM is None or _CONN_SEM_SIZE != max_conn:
            _CONN_SEM = threading.Semaphore(max(1, max_conn))
            _CONN_SEM_SIZE = max_conn
        return _CONN_SEM


def _flag(opts: dict, key: str, default: bool = True) -> bool:
    """
    读取布尔型配置项。

    配置文件里可能是 1/0、True/False，首选项 UI 存下来的是字符串
    "1"/"True"/"on"，这里统一解释，避免因为类型不同而失效。
    """
    raw = opts.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return bool(default)


def _num(opts: dict, key: str, default: float) -> float:
    """读取数值型配置项，非法值回落默认值。"""
    raw = opts.get(key, default)
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class DownloadTask:
    rel_path: str
    urls: list[str] = field(default_factory=list)
    sha1: str = ""
    sha256: str = ""
    sha512: str = ""
    file_size: int = 0


@dataclass
class DownloadResult:
    task: DownloadTask
    ok: bool
    target: Path | None = None
    error: str = ""
    aborted: bool = False


@dataclass
class _AttemptOutcome:
    ok: bool
    error: str = ""
    aborted: bool = False
    defer_to_single: bool = False
    fast_stalled: bool = False


@dataclass
class _PartResult:
    """单次「一个 URL 的一次尝试」的结果（内部使用）。"""
    ok: bool
    error: str = ""
    aborted: bool = False
    stalled: bool = False
    fast_stalled: bool = False
    range_unsupported: bool = False
    status_retryable: bool = False
    bytes_written: int = 0


# ----------------------------------------------------------------------
# 哈希 / 校验
# ----------------------------------------------------------------------
def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _verify(target: Path, task: DownloadTask) -> tuple[bool, str]:
    try:
        size = target.stat().st_size
    except OSError:
        return False, "文件不存在"
    if task.file_size and size != task.file_size:
        return False, f"大小不符（期望 {task.file_size}，实际 {size}）"
    if task.sha256:
        if _hash_file(target, "sha256") != task.sha256.lower():
            return False, "SHA-256 校验失败"
        return True, ""
    if task.sha512:
        if _hash_file(target, "sha512") != task.sha512.lower():
            return False, "SHA-512 校验失败"
        return True, ""
    if task.sha1:
        if _hash_file(target, "sha1") != task.sha1.lower():
            return False, "SHA-1 校验失败"
        return True, ""
    return True, ""


def _hash_spec(task: DownloadTask) -> tuple[str, str] | None:
    """本任务要求的哈希算法与期望值（边下边算用）。无要求返回 None。"""
    if task.sha256:
        return "sha256", task.sha256.lower()
    if task.sha512:
        return "sha512", task.sha512.lower()
    if task.sha1:
        return "sha1", task.sha1.lower()
    return None


# ----------------------------------------------------------------------
# A1：线程本地连接缓存
# ----------------------------------------------------------------------
_TLS = threading.local()

_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_LOCK = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    """
    G7：进程内共享一个 SSLContext。

    http.client.HTTPSConnection(context=None) 会在**每条连接**上新建
    SSLContext 并重载 CA 信任库，实测约 48ms/连接，且多线程下被 GIL 串行化。
    """
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = ssl.create_default_context()
        return _SSL_CONTEXT


def _tls_cache() -> dict:
    cache = getattr(_TLS, "conns", None)
    if cache is None:
        cache = {}
        _TLS.conns = cache
    return cache


def _get_conn(host: str, port: int, use_https: bool,
              timeout: float, opts: dict | None = None,
              fresh: bool = False) -> http.client.HTTPConnection:
    cache = _tls_cache()
    key = (host, port, use_https)
    if fresh:
        old = cache.pop(key, None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001, S110
                pass
    conn = None if fresh else cache.get(key)
    if conn is not None:
        try:
            if conn.sock is None:
                conn.connect()
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            conn = None
    if conn is None:
        if use_https:
            ctx = None
            if opts is None or _flag(opts, "shared_ssl_context", True):
                ctx = _ssl_context()
            conn = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(
                host, port, timeout=timeout)
        cache[key] = conn
    return conn


def _drop_conn(host: str, port: int, use_https: bool):
    cache = _tls_cache()
    conn = cache.pop((host, port, use_https), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


class _HttpStatusError(urlerr.URLError):
    """
    G2：非 2xx 响应。

    kind = "retry" → 408/425/429/5xx，换连接或稍后重试有意义；
    kind = "dead"  → 404/403 等，这个链接直接作废，换下一个源。
    """

    def __init__(self, status: int, url: str, kind: str = "dead",
                 snippet: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.url = url
        self.kind = kind
        self.snippet = snippet

    def __str__(self) -> str:
        tail = f"：{self.snippet}" if self.snippet else ""
        return f"{self.url} → HTTP {self.status}{tail}"


def _read_snippet(resp, limit: int = _ERROR_BODY_SNIPPET) -> str:
    """读取（最多 limit 字节的）错误页正文摘要，仅用于日志。"""
    try:
        data = resp.read(limit)
    except Exception:  # noqa: BLE001
        return ""
    if not data:
        return ""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(text.split())[:limit]


def _drain_quiet(resp, opts: dict, limit: float | None = None) -> bool:
    """
    尽力读完剩余响应体（重定向正文很小），超时即放弃。
    返回 True 表示已排空 → 连接可以继续复用。
    """
    try:
        sock = _get_socket(resp)
        if sock is not None:
            t = float(limit if limit is not None
                      else _num(opts, "release_response_timeout", 2.0))
            if t > 0:
                sock.settimeout(t)
        while True:
            if not resp.read(65536):
                return True
    except Exception:  # noqa: BLE001
        return False


def _release_response(key, resp, drained: bool):
    """
    G6：归还 / 丢弃一个响应。

    drained=True 表示响应体已读完 → 保留连接复用；
    否则（用户中止、停滞、分片提前收工）直接断开连接 —— 中止时最忌讳
    为了 keep-alive 去把服务器剩余的响应体读完（会白等一个 read_idle）。

    注意：这里必须显式 resp.close()。HTTPResponse.read1() 在读完整个
    正文时**不会**像 read() 那样调用 _close_conn()，于是 response 一直
    处于"未关闭"状态；下次 getresponse() 会因为 `self.__response` 非空
    抛 ResponseNotReady，keep-alive 复用直接失效（实测 12 个文件 = 12 条
    连接、且每个文件都要重试一次）。close() 只关闭读取缓冲，不动 socket。
    """
    if resp is not None:
        if drained:
            try:
                resp.close()
                return
            except Exception:  # noqa: BLE001, S110
                pass
        else:
            try:
                resp.close()
            except Exception:  # noqa: BLE001, S110
                pass
    if key is not None:
        try:
            _drop_conn(*key)
        except Exception:  # noqa: BLE001, S110
            pass


def _open_url_stream(url: str, headers: dict | None = None,
                     timeout: float = 10, opts: dict | None = None):
    """
    打开 URL 并返回 ((host, port, use_https), resp)。

    - 自动跟随最多 _REDIRECT_LIMIT 次重定向；
    - G2：非 2xx 抛 _HttpStatusError（只读 ≤512B 摘要，连接不复用）；
    - G9：陈旧 keep-alive 连接导致首个请求失败时，换新连接透明重试。
    """
    if opts is None:
        opts = _get_options()
    h = {
        "User-Agent": str(opts.get("user_agent", "PulsesEasier/1.0")),
        "Connection": "keep-alive",
        "Accept-Encoding": "identity",
    }
    if headers:
        h.update(headers)

    check_status = _flag(opts, "http_status_check", True)
    keepalive_retry = max(0, int(_num(opts, "keepalive_retry", 1)))

    current = url
    for _hop in range(_REDIRECT_LIMIT + 1):
        parsed = urllib.parse.urlsplit(current)
        if parsed.scheme not in ("http", "https"):
            raise urlerr.URLError(f"不支持的协议：{parsed.scheme}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        use_https = parsed.scheme == "https"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        key = (host, port, use_https)
        resp = None
        last_exc: Exception | None = None
        for attempt in range(keepalive_retry + 1):
            conn = _get_conn(host, port, use_https, timeout, opts,
                             fresh=(attempt > 0))
            try:
                conn.request("GET", path, headers=h)
                resp = conn.getresponse()
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                resp = None
                _drop_conn(host, port, use_https)
        if resp is None:
            raise urlerr.URLError(f"请求失败：{last_exc}")

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location", "")
            _release_response(key, resp, drained=_drain_quiet(resp, opts))
            if not location:
                raise urlerr.URLError("重定向缺少 Location")
            current = urllib.parse.urljoin(current, location)
            continue

        if check_status and not (200 <= resp.status < 300):
            snippet = _read_snippet(resp)
            _release_response(key, resp, drained=True)
            kind = "retry" if resp.status in _RETRYABLE_STATUS else "dead"
            raise _HttpStatusError(resp.status, current, kind, snippet)

        return key, resp

    raise urlerr.URLError("重定向次数过多")


# ----------------------------------------------------------------------
# 流工具
# ----------------------------------------------------------------------
def _set_socket_timeout(resp, timeout: float) -> bool:
    if timeout <= 0:
        return False
    try:
        sock = resp.fp.raw._sock  # type: ignore[attr-defined]
        sock.settimeout(timeout)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        sock = resp.fp  # type: ignore[attr-defined]
        sock.settimeout(timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_socket(resp):
    try:
        return resp.fp.raw._sock  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        return resp.fp  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


class _AbortError(Exception):
    def __init__(self, msg: str, aborted: bool = False,
                 stalled: bool = False, fast_stalled: bool = False):
        super().__init__(msg)
        self.aborted = aborted
        self.stalled = stalled
        self.fast_stalled = fast_stalled


def _is_abort(should_abort: Callable[[], bool] | None) -> bool:
    if should_abort is None:
        return False
    try:
        return bool(should_abort())
    except Exception:  # noqa: BLE001
        return False


class _RangeUnsupportedError(_AbortError):
    """G3：源没有按请求的 Range 返回数据（忽略 Range 或返回 200）。"""

    def __init__(self, msg: str):
        super().__init__(msg, stalled=False)
        self.range_unsupported = True


class _FileProgress:
    """
    G8：单个文件的进度聚合器（分片并发时对外单调，不回落）。

    value() = 已完成字节 + 所有在途分片的字节；
    分片失败时其"在途"字节被丢弃，但对外上报用 _high 夹住，保证不倒退。
    """

    __slots__ = ("rel_path", "total", "_byte_cb", "_lock", "_done",
                 "_inflight", "_high", "_last_ms", "_report_ms")

    def __init__(self, rel_path: str, total: int, byte_cb,
                 report_ms: float = BYTE_THROTTLE_MS):
        self.rel_path = rel_path
        self.total = int(total or 0)
        self._byte_cb = byte_cb
        self._lock = threading.Lock()
        self._done = 0
        self._inflight: dict[int, int] = {}
        self._high = 0
        self._last_ms = 0.0
        self._report_ms = max(0.0, float(report_ms))

    def value(self) -> int:
        with self._lock:
            return self._done + sum(self._inflight.values())

    def set_done(self, n: int):
        with self._lock:
            self._done = max(0, int(n))
            self._high = max(self._high, self._done)

    def add(self, part_id: int, n: int):
        """在途字节 +n（每读到一块调用一次），顺手按节流上报。"""
        if n <= 0:
            return
        with self._lock:
            self._inflight[part_id] = self._inflight.get(part_id, 0) + n
        self.report()

    def take(self, part_id: int):
        """把某个分片的在途字节并入"已完成"。"""
        with self._lock:
            self._done += self._inflight.pop(part_id, 0)
            self._high = max(self._high, self._done)

    def drop(self, part_id: int):
        """分片失败：丢弃其未完成部分（对外高水位不回退）。"""
        with self._lock:
            self._inflight.pop(part_id, None)

    def report(self, force: bool = False):
        cb = self._byte_cb
        if cb is None:
            return
        now_ms = time.time() * 1000.0
        raised = False
        with self._lock:
            cur = self._done + sum(self._inflight.values())
            if cur > self._high:
                self._high = cur
                raised = True
            cur = self._high
            if not force and not raised \
                    and now_ms - self._last_ms < self._report_ms:
                return
            self._last_ms = now_ms
        try:
            cb(self.rel_path, cur, self.total)
        except Exception:  # noqa: BLE001, S110
            pass


class _SpeedWindow:
    __slots__ = ("min_speed", "samples", "window")

    def __init__(self, window: float, min_speed: float):
        self.samples: deque[tuple[float, int]] = deque()
        self.window = window
        self.min_speed = min_speed

    def add(self, ts: float, total_bytes: int):
        self.samples.append((ts, total_bytes))
        while self.samples and ts - self.samples[0][0] > self.window:
            self.samples.popleft()

    def is_too_slow(self, now: float) -> bool:
        if len(self.samples) < 2:
            return False
        span = now - self.samples[0][0]
        if span < self.window * 0.8:
            return False
        gained = self.samples[-1][1] - self.samples[0][1]
        if gained <= 0:
            return True
        return (gained / span) < self.min_speed


# ----------------------------------------------------------------------
# 流式下载核心
# ----------------------------------------------------------------------
def _resp_drained(resp) -> bool:
    """响应体是否已经读完（读完才允许把连接留着复用）。"""
    try:
        if resp.isclosed():
            return True
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        return getattr(resp, "length", None) == 0
    except Exception:  # noqa: BLE001
        return False


def _content_range_ok(resp, start: int, end: int) -> bool:
    """G3：校验 Content-Range 与请求区间一致。"""
    cr = (resp.getheader("Content-Range") or "").strip()
    if not cr:
        # 没有该头 → 退化为「Content-Length 恰好等于区间长度」
        try:
            return int(resp.getheader("Content-Length", "0") or 0) \
                == end - start + 1
        except (TypeError, ValueError):
            return False
    try:
        unit, _, rng = cr.partition(" ")
        if unit.lower() != "bytes":
            return False
        span, _, _total = rng.partition("/")
        s_str, _, e_str = span.partition("-")
        return int(s_str) == start and int(e_str) == end
    except (TypeError, ValueError):
        return False


def _sleep_backoff(opts: dict, attempt: int):
    """指数退避 + 抖动（attempt 从 1 开始）。"""
    base = max(0.0, _num(opts, "retry_backoff_ms", 500) / 1000.0)
    if base <= 0:
        return
    cap = max(base, _num(opts, "backoff_max_ms", 8000) / 1000.0)
    delay = min(cap, base * (2 ** max(0, min(attempt - 1, 6))))
    jitter = max(0.0, min(1.0, _num(opts, "backoff_jitter", 0.3)))
    if jitter:
        delay *= (1.0 + random.uniform(-jitter, jitter))
    try:
        time.sleep(max(0.0, delay))
    except Exception:  # noqa: BLE001, S110
        pass


def _stream_to_file(resp, f, progress: _FileProgress | None, part_id: int,
                    url_start: float, url: str,
                    task: DownloadTask, total_size: int,
                    should_abort,
                    opts: dict,
                    single_mode: bool = False,
                    expected_bytes: int | None = None,
                    fast_stall: bool = False,
                    hasher=None) -> tuple[int, bool]:
    """
    流式写入 f，返回 (本次写入字节数, 是否已读完响应体)。

    G5：不再用 select() 探测原始 socket —— http.client 的 BufferedReader 会把
    响应体预读进用户态，select 看不见这些数据，于是把"正在下/已下完"误判成
    stalled。现在改为 socket 超时轮询 + read1()：每次读超时回到循环顶部，
    重新评估 停滞 / 单 URL 时限 / 速度窗口 / 用户中止。

    expected_bytes: 本片期望字节数（分片模式），到量即停。
    fast_stall: 多线程槽位专用快速筛除（连续 3s < 20KB/s → fast_stalled）。
    hasher: 可选，边下边算（仅当从 0 开始下整个文件时才可用）。
    """
    read_idle = _num(opts, "read_idle_timeout", 20)
    poll = _num(opts, "read_poll_interval", 1.0)
    if poll <= 0:
        poll = read_idle if read_idle > 0 else 30.0
    if read_idle > 0:
        poll = min(poll, read_idle)
    # 让阻塞读在 poll 秒后抛 socket.timeout，好回到循环顶部做各种判定
    _set_socket_timeout(resp, poll)

    if single_mode:
        stall_timeout = _num(opts, "single_stall_timeout", 30)
        per_url = _num(opts, "single_url_timeout", 180)
        min_speed = _num(opts, "single_min_speed_bps", 1024)
    else:
        stall_timeout = _num(opts, "stall_timeout", 10)
        per_url = _num(opts, "per_url_timeout", 90)
        min_speed = _num(opts, "min_speed_bps", 10240)

    speed_window = _SpeedWindow(
        _num(opts, "speed_window", 5.0), min_speed)

    # 快速筛除窗口（仅 fast_stall=True 时启用）
    fast_window = _SpeedWindow(FAST_STALL_WINDOW, FAST_STALL_MIN_BPS) \
        if fast_stall else None

    last_change_ts = time.time()
    written = 0
    drained = False

    while True:
        now = time.time()
        if _is_abort(should_abort):
            raise _AbortError("用户中止", aborted=True)
        if now - url_start > per_url:
            raise _AbortError(
                f"{url} → 超过单 URL 时限 {per_url:.0f}s", stalled=True)
        if now - last_change_ts > stall_timeout:
            raise _AbortError(
                f"{url} → 连续 {stall_timeout:.0f}s 无数据增长",
                stalled=True)
        if speed_window.is_too_slow(now):
            raise _AbortError(
                f"{url} → 速度低于 {min_speed:.0f} B/s 持续 "
                f"{speed_window.window:.0f}s", stalled=True)
        if fast_window is not None and fast_window.is_too_slow(now):
            raise _AbortError(
                f"{url} → 速度低于 {FAST_STALL_MIN_BPS // 1024} KB/s 持续 "
                f"{FAST_STALL_WINDOW:.0f}s（快速筛除）",
                stalled=True, fast_stalled=True)

        if expected_bytes is not None and written >= expected_bytes:
            drained = True
            break

        to_read = CHUNK_SIZE
        if expected_bytes is not None:
            to_read = min(CHUNK_SIZE, expected_bytes - written)
        if to_read <= 0:
            drained = True
            break

        try:
            chunk = resp.read1(to_read)
        except socket.timeout:
            # 到点还没数据 → 回到循环顶部重新评估（不再用 select 预判）
            continue
        except (urlerr.URLError, OSError) as e:
            raise _AbortError(f"{url} → 读取中断：{e}", stalled=True)

        if not chunk:
            drained = True
            break

        if hasher is not None:
            hasher.update(chunk)
        f.write(chunk)
        written += len(chunk)
        now2 = time.time()
        last_change_ts = now2
        speed_window.add(now2, written)
        if fast_window is not None:
            fast_window.add(now2, written)
        if progress is not None:
            progress.add(part_id, len(chunk))

    if expected_bytes is not None and written < expected_bytes:
        raise _AbortError(
            f"{url} → 数据不足（期望 {expected_bytes}，实收 {written}）",
            stalled=True)

    return written, drained


# ----------------------------------------------------------------------
# 单连接下载（支持断点续传）
# ----------------------------------------------------------------------
def _try_single_url(url: str, target: Path, part: Path,
                    task: DownloadTask,
                    progress: _FileProgress | None,
                    should_abort,
                    opts: dict,
                    single_mode: bool = False,
                    fast_stall: bool = False
                    ) -> _PartResult:
    """
    用单条连接下载（支持 Range 续传）。

    G4：续传起点只取 <target>.part.meta 认可的**连续前缀**；
        没有 meta 的 .part 一律从 0 重下，不再"按文件大小判定已下完"。
    G9：从 0 开始时边下边算哈希，省掉收尾时的一次全文件重读。
    """
    sem = _get_sem(int(_num(opts, "max_connections", 128)))
    headers: dict[str, str] = {}
    existing = 0

    if part.is_file():
        existing = _part_valid_prefix(part, opts)
        if _part_is_complete(part, task, opts):
            try:
                part.replace(target)
                return _PartResult(True)
            except OSError:
                existing = 0
        if existing <= 0:
            try:
                os.truncate(part, 0)
            except OSError:
                pass
        else:
            # 丢掉 meta 不认的尾部，保证"已下部分"是干净的前缀
            try:
                os.truncate(part, existing)
            except OSError:
                existing = 0
            else:
                headers["Range"] = f"bytes={existing}-"
    else:
        existing = 0

    url_start = time.time()
    with sem:
        if _is_abort(should_abort):
            return _PartResult(False, "用户中止", aborted=True)
        try:
            key, resp = _open_url_stream(
                url, headers=headers,
                timeout=_num(opts, "connect_timeout", 10), opts=opts)
        except _HttpStatusError as e:
            return _PartResult(False, str(e),
                               status_retryable=(e.kind == "retry"))
        except (urlerr.HTTPError, urlerr.URLError, OSError) as e:
            return _PartResult(False, f"{url} → 连接失败：{e}")

        drained = False
        written = 0
        try:
            total_size = task.file_size
            try:
                cr = resp.getheader("Content-Range")
                if cr and "/" in cr:
                    total_size = int(cr.rsplit("/", 1)[1])
                elif resp.getheader("Content-Length"):
                    cl = int(resp.getheader("Content-Length", "0") or 0)
                    total_size = (existing + cl
                                  if resp.status == 206 else cl)
            except Exception:  # noqa: BLE001, S110
                pass
            if not total_size:
                total_size = 0

            start_at = existing
            if start_at and resp.status != 206:
                # 服务器忽略 Range → 这份 .part 只能作废，从头下
                start_at = 0
                existing = 0
                try:
                    os.truncate(part, 0)
                except OSError:
                    pass

            if progress is not None:
                progress.set_done(start_at)

            hasher = None
            expected_digest: tuple[str, str] | None = None
            if start_at == 0 and _flag(opts, "hash_while_streaming", True):
                spec = _hash_spec(task)
                if spec is not None:
                    expected_digest = spec
                    hasher = hashlib.new(spec[0])

            expected: int | None = None
            if task.file_size:
                expected = task.file_size - start_at
                if expected <= 0:
                    expected = None

            part.parent.mkdir(parents=True, exist_ok=True)
            mode = "r+b" if (start_at and part.is_file()) else "w+b"
            try:
                with open(part, mode) as f:
                    if start_at:
                        f.seek(start_at)
                    written, drained = _stream_to_file(
                        resp, f, progress, 0, url_start, url,
                        task, total_size, should_abort, opts,
                        single_mode, expected_bytes=expected,
                        fast_stall=fast_stall, hasher=hasher)
            except _AbortError as e:
                n = start_at + written
                if written > 0 and _flag(opts, "part_meta_enabled", True):
                    _save_ranges(part, [(0, n - 1)], total_size)
                    if progress is not None:
                        progress.take(0)
                return _PartResult(
                    False, str(e),
                    aborted=getattr(e, "aborted", False),
                    stalled=getattr(e, "stalled", False),
                    fast_stalled=getattr(e, "fast_stalled", False),
                    bytes_written=written)
            drained = drained or _resp_drained(resp)

            n = start_at + written
            if _flag(opts, "part_meta_enabled", True) and n > 0:
                _save_ranges(part, [(0, n - 1)], total_size)

            if expected_digest is not None and hasher is not None:
                ok = hasher.hexdigest() == expected_digest[1]
                err = "" if ok else \
                    f"{expected_digest[0].upper()} 校验失败"
                if ok and task.file_size and n != task.file_size:
                    ok = False
                    err = (f"大小不符（期望 {task.file_size}，"
                           f"实际 {n}）")
            else:
                ok, err = _verify(part, task)

            if progress is not None:
                progress.take(0)
                progress.set_done(n)
                progress.report(force=True)

            if not ok:
                return _PartResult(False, f"{url} → {err}",
                                   bytes_written=written)
        finally:
            _release_response(key, resp, drained)

    try:
        part.replace(target)
    except OSError as e:
        return _PartResult(False, f"{url} → 移动失败：{e}")
    _drop_ranges(part)
    return _PartResult(True, bytes_written=written)


# ----------------------------------------------------------------------
# G4：.part 可信区间（sidecar 元数据）
#
# 为什么需要它：分片下载会把 <target>.part 预分配成和最终文件一样大
# （truncate / posix_fallocate），于是"文件大小 == 期望大小"完全不能说明
# 内容已经下全。旧实现据此直接把预分配出的零填充文件改名成正式文件，
# 在任务没带哈希时会被判为下载成功 —— 静默的数据损坏。
# 现在只承认 sidecar 里记录过的字节区间，续传只按连续前缀走。
# ----------------------------------------------------------------------
def _meta_path(part_file: Path) -> Path:
    return part_file.with_name(part_file.name + ".meta")


def _merge_ranges(ranges) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(ranges):
        if e < s:
            continue
        if out and s <= out[-1][1] + 1:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _covered_upto(limit: int, ranges) -> int:
    """从 0 开始连续覆盖到的字节数（遇到空洞即停）。"""
    pos = 0
    for s, e in ranges:
        if s > pos:
            break
        if e + 1 > pos:
            pos = e + 1
    if limit > 0:
        return min(pos, limit)
    return pos


def _range_covered(rng: tuple[int, int], ranges) -> bool:
    s, e = rng
    for cs, ce in ranges:
        if cs <= s and ce >= e:
            return True
        if cs > e:
            break
    return False


def _load_ranges(part_file: Path) -> tuple[list[tuple[int, int]], int]:
    """读取 sidecar；损坏/缺失一律返回 ([], 0)（= 不信任任何已有字节）。"""
    try:
        raw = _meta_path(part_file).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return [], 0
    if not isinstance(data, dict):
        return [], 0
    try:
        total = int(data.get("total", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    out: list[tuple[int, int]] = []
    items = data.get("ranges") or []
    if isinstance(items, list):
        for item in items:
            try:
                s = int(item[0])
                e = int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if e >= s >= 0:
                out.append((s, e))
    return _merge_ranges(out), total


def _save_ranges(part_file: Path, ranges, total: int):
    """原子写 sidecar（先写 .tmp 再 os.replace）。"""
    p = _meta_path(part_file)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(
            {"v": 1, "total": int(total or 0),
             "ranges": [[s, e] for s, e in _merge_ranges(ranges)]},
            ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001, S110
        pass


def _drop_ranges(part_file: Path):
    p = _meta_path(part_file)
    for target in (p, p.with_name(p.name + ".tmp")):
        try:
            target.unlink()
        except Exception:  # noqa: BLE001, S110
            pass


def _part_valid_prefix(part_file: Path, opts: dict) -> int:
    """
    返回 <target>.part 中可信的连续前缀长度。

    part_meta_enabled=1（默认）：只认 sidecar 记录的区间；
    part_meta_enabled=0：退回"按文件大小"的旧口径（仅影响续传起点，
                         不影响"是否已完成"的判定）。
    """
    if not _flag(opts, "part_meta_enabled", True):
        try:
            return max(0, part_file.stat().st_size)
        except OSError:
            return 0
    ranges, _total = _load_ranges(part_file)
    if not ranges:
        if _flag(opts, "part_meta_trust_size", False):
            try:
                return max(0, part_file.stat().st_size)
            except OSError:
                return 0
        return 0
    try:
        size = part_file.stat().st_size
    except OSError:
        return 0
    return min(_covered_upto(0, ranges), max(0, size))


def _part_is_complete(part_file: Path, task: DownloadTask,
                      opts: dict) -> bool:
    """
    只有 sidecar 明确记录了 [0, file_size) 才认为 .part 已下全。

    没有文件大小、或没开 sidecar 时一律返回 False：宁可重下，也不把
    预分配出来的零填充文件当成品。
    """
    if not task.file_size or not part_file.is_file():
        return False
    if not _flag(opts, "part_meta_enabled", True):
        return False
    ranges, _total = _load_ranges(part_file)
    if not ranges:
        return False
    return _covered_upto(0, ranges) >= task.file_size


_FALLOCATE = getattr(os, "posix_fallocate", None)


def _ensure_part_size(part_file: Path, total: int, opts: dict):
    """
    G9：把 .part 长度保证到 total（不破坏已有数据）。

    优先 os.posix_fallocate；不可用（典型：Windows）时回落 truncate。
    Windows 上 ftruncate 得到稀疏文件，边下边写可能碎片化，但语义正确。
    """
    try:
        if part_file.is_file() and part_file.stat().st_size >= total:
            return
    except OSError:
        pass

    if _flag(opts, "fallocate", True) and _FALLOCATE is not None:
        try:
            fd = os.open(part_file, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _FALLOCATE(fd, 0, total)
                return
            finally:
                os.close(fd)
        except OSError:
            pass

    if part_file.is_file():
        with open(part_file, "r+b") as f:
            f.truncate(total)
    else:
        with open(part_file, "wb") as f:
            f.truncate(total)


# ----------------------------------------------------------------------
# A：分片直写最终文件
# ----------------------------------------------------------------------
def _part_ranges(total: int, count: int) -> list[tuple[int, int]]:
    if count <= 1 or total <= 0:
        return [(0, total)]
    part_size = total // count
    if part_size < MIN_PART_SIZE:
        return [(0, total)]
    ranges: list[tuple[int, int]] = []
    for i in range(count):
        start = i * part_size
        end = (start + part_size - 1) if i < count - 1 else (total - 1)
        ranges.append((start, end))
    return ranges


def _download_part_to_file(url: str, part_file: Path,
                           start: int, end: int,
                           task: DownloadTask,
                           progress: _FileProgress | None,
                           file_total: int,
                           should_abort, opts: dict
                           ) -> _PartResult:
    sem = _get_sem(int(_num(opts, "max_connections", 128)))
    expected = end - start + 1

    headers = {"Range": f"bytes={start}-{end}"}

    url_start = time.time()
    with sem:
        if _is_abort(should_abort):
            return _PartResult(False, "用户中止", aborted=True)
        try:
            key, resp = _open_url_stream(
                url, headers=headers,
                timeout=_num(opts, "connect_timeout", 10), opts=opts)
        except _HttpStatusError as e:
            if e.status == 416:
                # 区间越界 → 远端实际长度比预期短
                return _PartResult(
                    False,
                    (f"{url} [片 {start}-{end}] → HTTP 416"
                     "（远端长度不足）"),
                    range_unsupported=True)
            return _PartResult(False, str(e),
                               status_retryable=(e.kind == "retry"))
        except (urlerr.HTTPError, urlerr.URLError, OSError) as e:
            return _PartResult(
                False, f"{url} [片 {start}-{end}] → 连接失败：{e}")

        drained = False
        written = 0
        try:
            # G3：Range 校验 —— 服务器忽略 Range 时如果照写，会把整份文件
            # 覆盖到错误偏移上，产出"校验能过但内容是错的"文件。
            # 注意：只有判定失败时才排空正文；正常路径必须原样交给流式写入。
            if _flag(opts, "range_validate", True):
                if resp.status != 206:
                    _release_response(key, resp,
                                      drained=_drain_quiet(resp, opts))
                    drained = True
                    return _PartResult(
                        False,
                        (f"{url} [片 {start}-{end}] → 该源忽略 Range"
                         f"（HTTP {resp.status}）"),
                        range_unsupported=True)
                if not _content_range_ok(resp, start, end):
                    _release_response(key, resp,
                                      drained=_drain_quiet(resp, opts))
                    drained = True
                    return _PartResult(
                        False,
                        (f"{url} [片 {start}-{end}] → Content-Range "
                         "与请求区间不一致"),
                        range_unsupported=True)

            try:
                with open(part_file, "r+b") as f:
                    f.seek(start)
                    written, drained = _stream_to_file(
                        resp, f, progress, start, url_start, url,
                        task, file_total, should_abort, opts,
                        single_mode=False,
                        expected_bytes=expected,
                        fast_stall=False)  # 分片内部不走快速筛除
            except _AbortError as e:
                if progress is not None:
                    progress.drop(start)
                return _PartResult(
                    False, str(e),
                    aborted=getattr(e, "aborted", False),
                    stalled=getattr(e, "stalled", False),
                    fast_stalled=getattr(e, "fast_stalled", False),
                    bytes_written=written)
            drained = drained or _resp_drained(resp)
        finally:
            _release_response(key, resp, drained)

    if written != expected:
        if progress is not None:
            progress.drop(start)
        return _PartResult(
            False,
            (f"{url} [片 {start}-{end}] → 字节数不足"
             f"（期望 {expected}，实收 {written}）"),
            bytes_written=written)

    if progress is not None:
        progress.take(start)
    return _PartResult(True, bytes_written=written)


def _download_multi_part(url: str, target: Path, task: DownloadTask,
                         total: int, progress: _FileProgress | None,
                         should_abort,
                         opts: dict) -> _PartResult:
    part_file = target.with_suffix(target.suffix + ".part")
    meta_on = _flag(opts, "part_meta_enabled", True)

    done_ranges: list[tuple[int, int]] = []
    if meta_on:
        raw, _t = _load_ranges(part_file)
        done_ranges = _merge_ranges(
            [(s, min(e, total - 1)) for s, e in raw if s < total])

    ranges = _part_ranges(total, MULTI_PART_COUNT)
    if len(ranges) <= 1:
        return _PartResult(False, "分片数不足")

    pending = [r for r in ranges if not _range_covered(r, done_ranges)]
    if not pending:
        ok, err = (_verify(part_file, task) if part_file.is_file()
                   else (False, "文件不存在"))
        if ok:
            _drop_ranges(part_file)
            try:
                part_file.replace(target)
                return _PartResult(True, bytes_written=total)
            except OSError as e:
                return _PartResult(False, f"{url} → 移动失败：{e}")
        return _PartResult(False, f"{url} → {err}")

    try:
        part_file.parent.mkdir(parents=True, exist_ok=True)
        _ensure_part_size(part_file, total, opts)
    except OSError as e:
        return _PartResult(False, f"预分配失败：{e}")

    if progress is not None:
        progress.set_done(sum(e - s + 1 for s, e in done_ranges))

    failures: list[_PartResult] = []
    result_lock = threading.Lock()
    cancel = threading.Event()
    retries = max(0, int(_num(opts, "part_retry", 1)))

    def _one(rng: tuple[int, int]):
        start, end = rng
        last = _PartResult(False, "未执行")
        for attempt in range(retries + 1):
            if cancel.is_set() or _is_abort(should_abort):
                return
            last = _download_part_to_file(
                url, part_file, start, end, task, progress,
                total, should_abort, opts)
            if last.ok:
                with result_lock:
                    done_ranges.append(rng)
                    if meta_on:
                        _save_ranges(part_file, done_ranges, total)
                return
            if last.aborted or last.range_unsupported:
                break
            if attempt < retries:
                _sleep_backoff(opts, attempt + 1)
        if last.range_unsupported:
            # 该源不支持分段 → 让其它分片尽快收手
            cancel.set()
        with result_lock:
            failures.append(last)

    workers = min(max(1, int(_num(opts, "part_threads", 4))), len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, r) for r in pending]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                with result_lock:
                    failures.append(
                        _PartResult(False, f"分片内部异常：{e}"))

    if any(f.aborted for f in failures):
        _drop_ranges(part_file)
        try:
            if part_file.is_file():
                part_file.unlink()
        except Exception:  # noqa: BLE001, S110
            pass
        return _PartResult(False, "用户中止", aborted=True)

    if failures:
        range_bad = any(f.range_unsupported for f in failures)
        if range_bad:
            # 内容已经写到错误偏移上了，只能整份作废，换源重来
            _drop_ranges(part_file)
            try:
                if part_file.is_file():
                    part_file.unlink()
            except Exception:  # noqa: BLE001, S110
                pass
            return _PartResult(False, failures[0].error,
                               range_unsupported=True,
                               stalled=any(f.stalled for f in failures))
        # 网络类失败：保留 .part + sidecar，下一个源可以接着下
        return _PartResult(
            False, f"{url} → 分片失败：{failures[0].error}",
            stalled=any(f.stalled for f in failures),
            fast_stalled=any(f.fast_stalled for f in failures),
            status_retryable=any(f.status_retryable for f in failures))

    _drop_ranges(part_file)
    try:
        part_file.replace(target)
        return _PartResult(True, bytes_written=total)
    except OSError as e:
        return _PartResult(False, f"{url} → 移动失败：{e}")


# ----------------------------------------------------------------------
# 单文件下载
# ----------------------------------------------------------------------
def _attempt_file(task: DownloadTask, target_dir: Path,
                  byte_cb, should_abort, opts: dict,
                  single_mode: bool = False
                  ) -> _AttemptOutcome:
    target = target_dir / task.rel_path
    part = target.with_suffix(target.suffix + ".part")

    try:
        return _attempt_file_inner(
            task, target_dir, byte_cb, should_abort, opts,
            single_mode, target, part)
    finally:
        _cleanup_parts(part)


def _attempt_file_inner(task: DownloadTask, target_dir: Path,
                        byte_cb, should_abort, opts: dict,
                        single_mode: bool,
                        target: Path, part: Path) -> _AttemptOutcome:
    if _is_abort(should_abort):
        return _AttemptOutcome(False, "用户中止", aborted=True)

    # G8：整个文件的进度聚合器（多分片并发时对外单调）
    progress = _FileProgress(task.rel_path, task.file_size, byte_cb)

    if target.is_file():
        ok, _err = _verify(target, task)
        if ok:
            if byte_cb and task.file_size:
                progress.set_done(task.file_size)
                progress.report(force=True)
            return _AttemptOutcome(True)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _AttemptOutcome(False, f"创建目录失败：{e}")

    if not task.urls:
        return _AttemptOutcome(False, "无下载链接")

    # G1：每个 URL 独立配额。
    # 旧实现把 single_fail_count 放在 URL 循环外，第 1 个 URL 用尽配额后
    # 直接 break —— task.urls 里第 2 个及以后的源永远不会被尝试。
    attempts_per_url = max(
        SINGLE_FAIL_BEFORE_MULTI,
        max(1, int(_num(opts, "retry_same_url", 1)) + 1))
    use_fast_stall = not single_mode
    multi_attempted = False
    last_err = "无可用下载链接"
    tried: list[str] = []

    for url in task.urls:
        if _is_abort(should_abort):
            return _AttemptOutcome(False, "用户中止", aborted=True)
        tried.append(url)

        # -------- 单连接：本 URL 独立配额 --------
        retryable_left = max(
            0, int(_num(opts, "retryable_status_retry", 1)))
        attempt = 0
        max_attempts = attempts_per_url
        while attempt < max_attempts:
            if _is_abort(should_abort):
                return _AttemptOutcome(False, "用户中止", aborted=True)

            res = _try_single_url(
                url, target, part, task, progress, should_abort, opts,
                single_mode=single_mode,
                fast_stall=use_fast_stall)
            if res.aborted:
                return _AttemptOutcome(False, "用户中止", aborted=True)
            if res.fast_stalled:
                # 快速筛除：直接返回，让上层投入单线程重试队列
                return _AttemptOutcome(False, res.error,
                                       fast_stalled=True)
            if res.ok:
                v_ok, v_err = _verify(target, task)
                if v_ok:
                    return _AttemptOutcome(True)
                last_err = f"{url} → {v_err}"
                try:
                    target.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                last_err = res.error
                # 5xx / 429 这类"服务器暂时不行"，额外给一次机会
                if res.status_retryable and retryable_left > 0:
                    retryable_left -= 1
                    max_attempts += 1

            attempt += 1
            if attempt < max_attempts:
                _sleep_backoff(opts, attempt)

        # -------- 分片：整个文件只尝试一次 --------
        if (not single_mode
                and not multi_attempted
                and task.file_size >= MULTI_PART_THRESHOLD):
            multi_attempted = True
            res = _download_multi_part(
                url, target, task, task.file_size,
                progress, should_abort, opts)
            if res.aborted:
                return _AttemptOutcome(False, "用户中止", aborted=True)
            if res.fast_stalled:
                return _AttemptOutcome(False, res.error,
                                       fast_stalled=True)
            if res.range_unsupported:
                # G3：这个源不支持分段 → 换下一个源继续试（不再写坏文件）
                last_err = res.error
                continue
            if res.ok:
                v_ok, v_err = _verify(target, task)
                if v_ok:
                    return _AttemptOutcome(True)
                last_err = f"{url} → {v_err}"
                try:
                    target.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                last_err = res.error

    if len(tried) > 1:
        last_err = f"{last_err}（已尝试 {len(tried)} 个源）"
    return _AttemptOutcome(False, last_err)


def _cleanup_parts(part: Path):
    """清掉某个文件的 .part、.part.meta 以及历史遗留的 .part.N 中间文件。"""
    _drop_ranges(part)
    try:
        if part.is_file():
            part.unlink()
    except Exception:  # noqa: BLE001, S110
        pass
    parent = part.parent
    stem = part.name
    try:
        for p in parent.iterdir():
            if p.name.startswith(stem + ".") and ".part." in p.name:
                try:
                    p.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
    except Exception:  # noqa: BLE001, S110
        pass


# ----------------------------------------------------------------------
# 两级槽位池主入口
# ----------------------------------------------------------------------
def download_files(
    tasks: list[DownloadTask],
    target_dir: Path,
    threads: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    log: Callable[[str, str], None] | None = None,
    byte_progress: Callable[[str, int, int], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_file_started: Callable[[DownloadTask], None] | None = None,
    on_file_done: Callable[[DownloadTask], None] | None = None,
    on_file_failed: Callable[[DownloadTask, str], None] | None = None,
) -> list[DownloadResult]:
    opts = _get_options()
    multi_slots = int(opts.get("multi_slots", 12))
    single_slots = int(opts.get("single_slots", 4))
    if threads is not None and threads > 0:
        multi_slots = max(1, int(threads))

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    total = len(tasks)
    if total == 0:
        if progress:
            try:
                progress(0, 0, "")
            except Exception:  # noqa: BLE001, S110
                pass
        return []

    retry_queue: Queue[DownloadTask] = Queue()
    result_lock = threading.Lock()
    done_lock = threading.Lock()
    done_counter = {"n": 0}
    last_report = {"n": 0}
    outcome_map: dict[int, DownloadResult] = {}

    def _store(task: DownloadTask, res: DownloadResult):
        with result_lock:
            outcome_map[id(task)] = res

    def _report_final(task: DownloadTask, ok: bool, err: str,
                      aborted: bool = False):
        """
        一个文件的**最终**状态才走这里。

        旧实现把"转入单线程重试"也当成一次完成来报，结果进度条的
        done 会超过 total，且被救回的文件在返回列表里仍留一条失败记录。
        """
        with done_lock:
            done_counter["n"] += 1
            done = done_counter["n"]
            if progress and (done - last_report["n"] >= PROGRESS_THROTTLE
                             or done == total):
                last_report["n"] = done
                try:
                    progress(done, total, task.rel_path)
                except Exception:  # noqa: BLE001, S110
                    pass
        if log and not aborted:
            if ok:
                log("info", f"下载完成 {task.rel_path}")
            else:
                log("error", f"下载失败 {task.rel_path}：{err}")
        if on_file_done is not None:
            try:
                on_file_done(task)
            except Exception:  # noqa: BLE001, S110
                pass

    def _run_multi(task: DownloadTask) -> DownloadResult:
        if _is_abort(should_abort):
            res = DownloadResult(task, False, None, "用户中止",
                                 aborted=True)
            _store(task, res)
            _report_final(task, False, "用户中止", aborted=True)
            return res
        if on_file_started is not None:
            try:
                on_file_started(task)
            except Exception:  # noqa: BLE001, S110
                pass

        try:
            outcome = _attempt_file(task, target_dir, byte_progress,
                                    should_abort, opts,
                                    single_mode=False)
        except Exception as e:  # noqa: BLE001
            outcome = _AttemptOutcome(
                False, f"内部异常：{type(e).__name__}: {e}")

        if outcome.aborted:
            res = DownloadResult(task, False, None, "用户中止",
                                 aborted=True)
            _store(task, res)
            _report_final(task, False, "用户中止", aborted=True)
            return res

        if outcome.ok:
            target = target_dir / task.rel_path
            res = DownloadResult(task, True, target, "")
            _store(task, res)
            _report_final(task, True, "")
            return res

        # 转入单线程重试槽位：**不算最终态**，进度与收尾回调都不动
        if log:
            try:
                log("warn", f"{task.rel_path} 转入单线程重试队列")
            except Exception:  # noqa: BLE001, S110
                pass
        retry_queue.put(task)
        res = DownloadResult(task, False, None, outcome.error)
        _store(task, res)
        return res

    try:
        multi_workers = max(1, multi_slots)
        with ThreadPoolExecutor(max_workers=multi_workers) as pool:
            futures = {pool.submit(_run_multi, t): t for t in tasks}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    t = futures[fut]
                    res = DownloadResult(t, False, None, str(e))
                    _store(t, res)
                    _report_final(t, False, str(e))
    except Exception as e:  # noqa: BLE001
        if log:
            try:
                log("error", f"多线程槽位池异常：{e}")
            except Exception:  # noqa: BLE001, S110
                pass

    # -------------------- 单线程重试槽位 --------------------
    retry_tasks: list[DownloadTask] = []
    while not retry_queue.empty():
        try:
            retry_tasks.append(retry_queue.get_nowait())
        except Exception:  # noqa: BLE001
            break

    if retry_tasks and not _is_abort(should_abort):
        if log:
            try:
                log("info",
                    f"{len(retry_tasks)} 个文件进入单线程重试阶段")
            except Exception:  # noqa: BLE001, S110
                pass

        def _run_single(task: DownloadTask) -> DownloadResult:
            if _is_abort(should_abort):
                res = DownloadResult(task, False, None, "用户中止",
                                     aborted=True)
                _store(task, res)
                _report_final(task, False, "用户中止", aborted=True)
                return res
            if on_file_started is not None:
                try:
                    on_file_started(task)
                except Exception:  # noqa: BLE001, S110
                    pass
            try:
                outcome = _attempt_file(task, target_dir, byte_progress,
                                        should_abort, opts,
                                        single_mode=True)
            except Exception as e:  # noqa: BLE001
                outcome = _AttemptOutcome(
                    False, f"内部异常：{type(e).__name__}: {e}")

            if outcome.aborted:
                res = DownloadResult(task, False, None, "用户中止",
                                     aborted=True)
                _store(task, res)
                _report_final(task, False, "用户中止", aborted=True)
                return res
            if outcome.ok:
                target = target_dir / task.rel_path
                res = DownloadResult(task, True, target, "")
                _store(task, res)
                _report_final(task, True, "")
                return res

            if on_file_failed is not None:
                try:
                    on_file_failed(task, outcome.error)
                except Exception:  # noqa: BLE001, S110
                    pass
            res = DownloadResult(task, False, None, outcome.error)
            _store(task, res)
            _report_final(task, False, outcome.error)
            return res

        try:
            single_workers = max(1, single_slots)
            with ThreadPoolExecutor(max_workers=single_workers) as pool:
                futures = {pool.submit(_run_single, t): t
                           for t in retry_tasks}
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        t = futures[fut]
                        res = DownloadResult(t, False, None, str(e))
                        _store(t, res)
                        _report_final(t, False, str(e))
        except Exception as e:  # noqa: BLE001
            if log:
                try:
                    log("error", f"单线程重试池异常：{e}")
                except Exception:  # noqa: BLE001, S110
                    pass
    elif retry_tasks:
        for t in retry_tasks:
            res = DownloadResult(t, False, None, "用户中止",
                                 aborted=True)
            _store(t, res)
            _report_final(t, False, "用户中止", aborted=True)

    if _flag(opts, "cleanup_residual_parts", True):
        _cleanup_residual_parts(target_dir)

    # 每个任务返回**一条最终结果**（成功 + 失败）。
    # 调用方 player_view 的统计口径是 len(results) - len(failed)，
    # 旧实现只回失败项：于是「成功 N」永远是 0，且被降级救回的文件
    # 仍然留在失败列表里、_completed_files 也永远收集不到东西。
    results: list[DownloadResult] = []
    for t in tasks:
        res = outcome_map.get(id(t))
        if res is None:
            res = DownloadResult(t, False, None, "未处理",
                                 aborted=_is_abort(should_abort))
            _report_final(t, False, "未处理",
                          aborted=res.aborted)
        results.append(res)
    return results


def _cleanup_residual_parts(target_dir: Path):
    try:
        for p in target_dir.rglob("*.part*"):
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".part") or ".part." in name:
                try:
                    p.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
    except Exception:  # noqa: BLE001, S110
        pass


def estimate_eta(done: int, total: int, started_at: float) -> int:
    if done <= 0 or total <= 0:
        return 0
    elapsed = max(0.001, time.time() - started_at)
    rate = done / elapsed
    if rate <= 0:
        return 0
    return int(max(0, total - done) / rate)
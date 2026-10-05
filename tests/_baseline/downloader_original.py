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
"""

import hashlib
import http.client
import select
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

from app.core import database as db

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
_ABORT_POLL_INTERVAL = 0.5

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
        if _hash_file(target, "sha256") != task.sha256:
            return False, "SHA-256 校验失败"
        return True, ""
    if task.sha512:
        if _hash_file(target, "sha512") != task.sha512:
            return False, "SHA-512 校验失败"
        return True, ""
    if task.sha1:
        if _hash_file(target, "sha1") != task.sha1:
            return False, "SHA-1 校验失败"
        return True, ""
    return True, ""


# ----------------------------------------------------------------------
# A1：线程本地连接缓存
# ----------------------------------------------------------------------
_TLS = threading.local()


def _tls_cache() -> dict:
    cache = getattr(_TLS, "conns", None)
    if cache is None:
        cache = {}
        _TLS.conns = cache
    return cache


def _get_conn(host: str, port: int, use_https: bool,
              timeout: float) -> http.client.HTTPConnection:
    cache = _tls_cache()
    key = (host, port, use_https)
    conn = cache.get(key)
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
            conn = http.client.HTTPSConnection(
                host, port, timeout=timeout)
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


def _open_url_stream(url: str, headers: dict | None = None,
                     timeout: float = 10):
    h = {"User-Agent": "PulsesEasier/1.0",
         "Connection": "keep-alive"}
    if headers:
        h.update(headers)

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

        conn = _get_conn(host, port, use_https, timeout)
        try:
            conn.request("GET", path, headers=h)
            resp = conn.getresponse()
        except Exception as e:
            _drop_conn(host, port, use_https)
            raise urlerr.URLError(f"请求失败：{e}") from e

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location", "")
            try:
                resp.read()
            except Exception:  # noqa: BLE001, S110
                pass
            if not location:
                raise urlerr.URLError("重定向缺少 Location")
            current = urllib.parse.urljoin(current, location)
            continue

        return (host, port, use_https), resp

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


def _wait_readable(sock, timeout: float,
                   should_abort) -> tuple[bool, bool]:
    waited = 0.0
    while waited < timeout:
        if _is_abort(should_abort):
            return False, True
        try:
            r, _, _ = select.select(
                [sock], [], [],
                min(_ABORT_POLL_INTERVAL, timeout - waited))
        except (OSError, ValueError):
            return True, False
        if r:
            return True, False
        waited += _ABORT_POLL_INTERVAL
    return False, False


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
def _stream_to_file(resp, f, byte_counter: dict,
                    url_start: float, url: str,
                    byte_cb, task, total_size: int,
                    last_cb: list[float],
                    should_abort,
                    opts: dict,
                    single_mode: bool = False,
                    expected_bytes: int | None = None,
                    fast_stall: bool = False):
    """
    流式写入 f。
    expected_bytes: 本片期望字节数（分片模式），用于停止条件。
    fast_stall: True 时启用快速筛除（多线程槽位专用）：
                连续 3s 平均速度 < 20KB/s → 抛 fast_stalled。
    """
    sock = _get_socket(resp)
    read_idle = float(opts.get("read_idle_timeout", 20))

    if single_mode:
        stall_timeout = float(opts.get("single_stall_timeout", 30))
        per_url = float(opts.get("single_url_timeout", 180))
        min_speed = float(opts.get("single_min_speed_bps", 1024))
    else:
        stall_timeout = float(opts.get("stall_timeout", 10))
        per_url = float(opts.get("per_url_timeout", 90))
        min_speed = float(opts.get("min_speed_bps", 10240))

    speed_window = _SpeedWindow(
        float(opts.get("speed_window", 5.0)), min_speed)

    # 快速筛除窗口（仅 fast_stall=True 时启用）
    fast_window = _SpeedWindow(FAST_STALL_WINDOW, FAST_STALL_MIN_BPS) \
        if fast_stall else None

    last_change_ts = time.time()
    written = 0

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
            break

        if sock is not None:
            got_data, aborted = _wait_readable(
                sock, read_idle, should_abort)
            if aborted:
                raise _AbortError("用户中止", aborted=True)
            if not got_data:
                raise _AbortError(
                    f"{url} → 读空闲超过 {read_idle:.0f}s",
                    stalled=True)

        try:
            to_read = CHUNK_SIZE
            if expected_bytes is not None:
                to_read = min(CHUNK_SIZE, expected_bytes - written)
            chunk = resp.read(to_read)
        except (urlerr.URLError, OSError) as e:
            raise _AbortError(f"{url} → 读取中断：{e}", stalled=True)

        if not chunk:
            break

        f.write(chunk)
        written += len(chunk)
        byte_counter["n"] += len(chunk)
        now2 = time.time()
        last_change_ts = now2
        speed_window.add(now2, byte_counter["n"])
        if fast_window is not None:
            fast_window.add(now2, byte_counter["n"])

        if byte_cb is not None:
            now_ms = now2 * 1000
            if now_ms - last_cb[0] >= BYTE_THROTTLE_MS:
                last_cb[0] = now_ms
                try:
                    byte_cb(task.rel_path, byte_counter["n"], total_size)
                except Exception:  # noqa: BLE001, S110
                    pass

    if expected_bytes is not None and written < expected_bytes:
        raise _AbortError(
            f"{url} → 数据不足（期望 {expected_bytes}，实收 {written}）",
            stalled=True)


# ----------------------------------------------------------------------
# 单连接下载（支持断点续传）
# ----------------------------------------------------------------------
def _try_single_url(url: str, target: Path, part: Path,
                    task: DownloadTask,
                    byte_cb, should_abort,
                    opts: dict,
                    single_mode: bool = False,
                    fast_stall: bool = False
                    ) -> tuple[bool, str, bool, bool, bool]:
    """
    返回 (ok, err, aborted, stalled, fast_stalled)
    """
    sem = _get_sem(int(opts.get("max_connections", 128)))
    headers: dict[str, str] = {}
    existing_size = 0
    if part.is_file():
        try:
            existing_size = part.stat().st_size
            if task.file_size and existing_size >= task.file_size:
                try:
                    part.replace(target)
                    return True, "", False, False, False
                except OSError:
                    existing_size = 0
            elif existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"
        except OSError:
            existing_size = 0

    url_start = time.time()
    with sem:
        if _is_abort(should_abort):
            return False, "用户中止", True, False, False
        try:
            _host_key, resp = _open_url_stream(
                url, headers=headers,
                timeout=float(opts.get("connect_timeout", 10)))
        except (urlerr.HTTPError, urlerr.URLError, OSError) as e:
            return False, f"{url} → 连接失败：{e}", False, False, False

        try:
            total_size = task.file_size
            try:
                cr = resp.getheader("Content-Range")
                if cr and "/" in cr:
                    total_size = int(cr.rsplit("/", 1)[1])
                elif resp.getheader("Content-Length"):
                    cl = int(resp.getheader("Content-Length", "0") or 0)
                    total_size = (existing_size + cl
                                  if resp.status == 206 else cl)
            except Exception:  # noqa: BLE001, S110
                pass

            _set_socket_timeout(resp, float(
                opts.get("read_idle_timeout", 20)))

            mode = "ab" if existing_size > 0 and resp.status == 206 else "wb"
            if mode == "ab" and resp.status != 206:
                mode = "wb"
                existing_size = 0

            counter = {"n": existing_size}
            last_cb = [0.0]

            if byte_cb is not None:
                try:
                    byte_cb(task.rel_path, counter["n"], total_size)
                except Exception:  # noqa: BLE001, S110
                    pass

            try:
                with open(part, mode) as f:
                    _stream_to_file(resp, f, counter, url_start, url,
                                    byte_cb, task, total_size, last_cb,
                                    should_abort, opts, single_mode,
                                    fast_stall=fast_stall)
            except _AbortError as e:
                return (False, str(e),
                        getattr(e, "aborted", False),
                        getattr(e, "stalled", False),
                        getattr(e, "fast_stalled", False))
            finally:
                try:
                    resp.read()
                except Exception:  # noqa: BLE001, S110
                    pass
        finally:
            pass

    try:
        part.replace(target)
    except OSError as e:
        return False, f"{url} → 移动失败：{e}", False, False, False
    return True, "", False, False, False


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
                           task: DownloadTask, byte_cb,
                           file_total: int,
                           done_counter: dict,
                           counter_lock: threading.Lock,
                           last_cb: list[float],
                           should_abort, opts: dict
                           ) -> tuple[bool, str, bool, bool, bool]:
    sem = _get_sem(int(opts.get("max_connections", 128)))
    expected = end - start + 1

    headers = {
        "User-Agent": "PulsesEasier/1.0",
        "Range": f"bytes={start}-{end}",
        "Connection": "keep-alive",
    }

    url_start = time.time()
    with sem:
        if _is_abort(should_abort):
            return False, "用户中止", True, False, False
        try:
            _host_key, resp = _open_url_stream(
                url, headers=headers,
                timeout=float(opts.get("connect_timeout", 10)))
        except (urlerr.HTTPError, urlerr.URLError, OSError) as e:
            return False, f"{url} [片 {start}-{end}] → 连接失败：{e}", False, False, False

        try:
            _set_socket_timeout(resp, float(
                opts.get("read_idle_timeout", 20)))
            counter = {"n": 0}
            last_cb_local = [0.0]

            # 分片启动：立即报一次全局进度
            if byte_cb is not None:
                with counter_lock:
                    cur = done_counter["n"]
                try:
                    byte_cb(task.rel_path, cur, file_total)
                except Exception:  # noqa: BLE001, S110
                    pass

            try:
                with open(part_file, "r+b") as f:
                    f.seek(start)
                    _stream_to_file(
                        resp, f, counter, url_start, url,
                        byte_cb, task, file_total,
                        last_cb_local, should_abort, opts,
                        single_mode=False,
                        expected_bytes=expected,
                        fast_stall=False)  # 分片内部不走快速筛除
            except _AbortError as e:
                return (False, str(e),
                        getattr(e, "aborted", False),
                        getattr(e, "stalled", False),
                        getattr(e, "fast_stalled", False))
            finally:
                try:
                    resp.read()
                except Exception:  # noqa: BLE001, S110
                    pass
        finally:
            pass

    if counter["n"] != expected:
        return (False,
                (f"{url} [片 {start}-{end}] → 字节数不足"
                f"（期望 {expected}，实收 {counter['n']}）"),
                False, False, False)

    with counter_lock:
        done_counter["n"] += expected
        if byte_cb is not None:
            try:
                byte_cb(task.rel_path, done_counter["n"], file_total)
            except Exception:  # noqa: BLE001, S110
                pass
    return True, "", False, False, False


def _download_multi_part(url: str, target: Path, task: DownloadTask,
                         total: int, byte_cb, should_abort,
                         opts: dict) -> tuple[bool, str, bool, bool, bool]:
    ranges = _part_ranges(total, MULTI_PART_COUNT)
    if len(ranges) <= 1:
        return False, "分片数不足", False, False, False

    part_file = target.with_suffix(target.suffix + ".part")

    try:
        part_file.parent.mkdir(parents=True, exist_ok=True)
        with open(part_file, "wb") as f:
            f.truncate(total)
    except OSError as e:
        return False, f"预分配失败：{e}", False, False, False

    done_counter = {"n": 0}
    counter_lock = threading.Lock()
    last_cb = [0.0]

    errors: list[str] = []
    aborted_flag = [False]
    stalled_flag = [False]
    fast_stalled_flag = [False]

    def _one(idx: int):
        start, end = ranges[idx]
        return _download_part_to_file(
            url, part_file, start, end, task,
            byte_cb, total, done_counter, counter_lock,
            last_cb, should_abort, opts)

    workers = min(int(opts.get("part_threads", 4)), len(ranges))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, i) for i in range(len(ranges))]
        for fut in as_completed(futures):
            try:
                ok, err, ab, st, fst = fut.result()
            except Exception as e:  # noqa: BLE001
                ok, err, ab, st, fst = False, str(e), False, False, False
            if ab:
                aborted_flag[0] = True
            if st:
                stalled_flag[0] = True
            if fst:
                fast_stalled_flag[0] = True
            if not ok:
                errors.append(err)

    if aborted_flag[0]:
        try:
            if part_file.is_file():
                part_file.unlink()
        except Exception:  # noqa: BLE001, S110
            pass
        return False, "用户中止", True, False, False

    if errors:
        try:
            if part_file.is_file():
                part_file.unlink()
        except Exception:  # noqa: BLE001, S110
            pass
        return (False, f"{url} → 分片失败：{errors[0]}",
                False, stalled_flag[0], fast_stalled_flag[0])

    try:
        part_file.replace(target)
        return True, "", False, False, False
    except OSError as e:
        try:
            if part_file.is_file():
                part_file.unlink()
        except Exception:  # noqa: BLE001, S110
            pass
        return False, f"{url} → 移动失败：{e}", False, False, False


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

    if target.is_file():
        ok, _err = _verify(target, task)
        if ok:
            if byte_cb and task.file_size:
                try:
                    byte_cb(task.rel_path, task.file_size, task.file_size)
                except Exception:  # noqa: BLE001, S110
                    pass
            return _AttemptOutcome(True)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _AttemptOutcome(False, f"创建目录失败：{e}")

    if not task.urls:
        return _AttemptOutcome(False, "无下载链接")

    backoff_ms = int(opts.get("retry_backoff_ms", 500))
    single_fail_count = 0
    multi_attempted = False
    last_err = "无可用下载链接"

    # 多线程槽位（非 single_mode）启用快速筛除
    use_fast_stall = not single_mode

    for url in task.urls:
        if _is_abort(should_abort):
            return _AttemptOutcome(False, "用户中止", aborted=True)

        # -------- 第 1、2 次：单连接 --------
        while single_fail_count < SINGLE_FAIL_BEFORE_MULTI:
            if _is_abort(should_abort):
                return _AttemptOutcome(False, "用户中止", aborted=True)

            ok, err, ab, _st, fst = _try_single_url(
                url, target, part, task, byte_cb, should_abort, opts,
                single_mode=single_mode,
                fast_stall=use_fast_stall)
            if ab:
                return _AttemptOutcome(False, "用户中止", aborted=True)
            if fst:
                # 快速筛除：直接返回，让上层投入单线程重试队列
                return _AttemptOutcome(
                    False, err, fast_stalled=True)
            if ok:
                v_ok, v_err = _verify(target, task)
                if v_ok:
                    return _AttemptOutcome(True)
                last_err = f"{url} → {v_err}"
                try:
                    target.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                last_err = err
            single_fail_count += 1

            if (single_fail_count < SINGLE_FAIL_BEFORE_MULTI
                    and backoff_ms > 0):
                try:
                    time.sleep(backoff_ms / 1000.0)
                except Exception:  # noqa: BLE001, S110
                    pass

        # -------- 第 3 次：分片（若满足阈值） --------
        if (not single_mode
                and not multi_attempted
                and task.file_size >= MULTI_PART_THRESHOLD):
            multi_attempted = True
            ok, err, ab, _st, fst = _download_multi_part(
                url, target, task, task.file_size,
                byte_cb, should_abort, opts)
            if ab:
                return _AttemptOutcome(False, "用户中止", aborted=True)
            if fst:
                return _AttemptOutcome(
                    False, err, fast_stalled=True)
            if ok:
                v_ok, v_err = _verify(target, task)
                if v_ok:
                    return _AttemptOutcome(True)
                last_err = f"{url} → {v_err}"
                try:
                    target.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                last_err = err
            break

        break

    return _AttemptOutcome(False, last_err)


def _cleanup_parts(part: Path):
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

    results: list[DownloadResult] = []
    retry_queue: Queue[DownloadTask] = Queue()
    result_lock = threading.Lock()
    done_lock = threading.Lock()
    done_counter = {"n": 0}
    last_report = {"n": 0}

    def _report_done(task: DownloadTask, ok: bool, err: str,
                     aborted: bool, deferred: bool):
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
                elif deferred:
                    log("warn",
                        f"{task.rel_path} 转入单线程重试队列")
                else:
                    log("error",
                        f"下载失败 {task.rel_path}：{err}")

    def _run_multi(task: DownloadTask) -> DownloadResult:
        if _is_abort(should_abort):
            return DownloadResult(task, False, None, "用户中止",
                                  aborted=True)
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
            outcome = _AttemptOutcome(False, f"内部异常：{e}")

        if outcome.aborted:
            if on_file_done is not None:
                try:
                    on_file_done(task)
                except Exception:  # noqa: BLE001, S110
                    pass
            return DownloadResult(task, False, None, "用户中止",
                                  aborted=True)

        if outcome.ok:
            _report_done(task, True, "", False, False)
            if on_file_done is not None:
                try:
                    on_file_done(task)
                except Exception:  # noqa: BLE001, S110
                    pass
            target = target_dir / task.rel_path
            return DownloadResult(task, True, target, "")

        retry_queue.put(task)
        _report_done(task, False, outcome.error, False, True)
        if on_file_done is not None:
            try:
                on_file_done(task)
            except Exception:  # noqa: BLE001, S110
                pass
        return DownloadResult(task, False, None, outcome.error)

    try:
        multi_workers = max(1, multi_slots)
        with ThreadPoolExecutor(max_workers=multi_workers) as pool:
            futures = [pool.submit(_run_multi, t) for t in tasks]
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = DownloadResult(DownloadTask(rel_path="?"), False,
                                         None, str(e))
                with result_lock:
                    if not res.ok:
                        results.append(res)
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
                return DownloadResult(task, False, None, "用户中止",
                                      aborted=True)
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
                outcome = _AttemptOutcome(False, f"内部异常：{e}")

            if outcome.aborted:
                if on_file_done is not None:
                    try:
                        on_file_done(task)
                    except Exception:  # noqa: BLE001, S110
                        pass
                return DownloadResult(task, False, None, "用户中止",
                                      aborted=True)
            if outcome.ok:
                _report_done(task, True, "", False, False)
                if on_file_done is not None:
                    try:
                        on_file_done(task)
                    except Exception:  # noqa: BLE001, S110
                        pass
                target = target_dir / task.rel_path
                return DownloadResult(task, True, target, "")

            if on_file_failed is not None:
                try:
                    on_file_failed(task, outcome.error)
                except Exception:  # noqa: BLE001, S110
                    pass
            _report_done(task, False, outcome.error, False, False)
            if on_file_done is not None:
                try:
                    on_file_done(task)
                except Exception:  # noqa: BLE001, S110
                    pass
            return DownloadResult(task, False, None, outcome.error)

        try:
            single_workers = max(1, single_slots)
            with ThreadPoolExecutor(max_workers=single_workers) as pool:
                futures = [pool.submit(_run_single, t) for t in retry_tasks]
                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        res = DownloadResult(DownloadTask(rel_path="?"),
                                             False, None, str(e))
                    with result_lock:
                        if not res.ok:
                            results.append(res)
        except Exception as e:  # noqa: BLE001
            if log:
                try:
                    log("error", f"单线程重试池异常：{e}")
                except Exception:  # noqa: BLE001, S110
                    pass
    elif retry_tasks:
        for t in retry_tasks:
            results.append(DownloadResult(t, False, None, "用户中止",
                                          aborted=True))
            if on_file_done is not None:
                try:
                    on_file_done(t)
                except Exception:  # noqa: BLE001, S110
                    pass

    _cleanup_residual_parts(target_dir)

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
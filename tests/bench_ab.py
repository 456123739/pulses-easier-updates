#!/usr/bin/env python3
"""
bench_ab.py — 原版 downloader vs 改进版 downloader 的成对 A/B 对比。

原版：tests/_baseline/downloader_original.py（上传 ZIP 里的原始文件，逐字副本）
改进版：app/core/downloader.py

两个实现跑同一套本地模拟源（fixture_server），交替执行、取中位数。
HTTPS 场景用自签证书 + SSL_CERT_FILE，用来隔离「共享 SSLContext」的收益。

关于 read_idle_timeout：为了让对比能在有限时间内跑完，计时场景给**两边**
都显式设成 3 秒。这对改进版是"无用参数"（它不做 select 预判，不会误判），
对原版则是**放宽**（原版默认 20 秒）——所以测出来的加速比是真实收益的
**下界**。另外单独跑一组"原版默认参数"的场景给出真实默认值下的代价。

运行：  python3 tests/bench_ab.py
"""

import hashlib
import importlib.util
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

_TLS = _HERE / "_tls"
if not (_TLS / "cert.pem").is_file():
    # 证书不进版本库：缺失时现场生成（没有 openssl/cryptography 就跳过 HTTPS）
    try:
        from make_tls import ensure_certs
        ensure_certs()
    except Exception:  # noqa: BLE001, S110
        pass
if (_TLS / "cert.pem").is_file():
    os.environ.setdefault("SSL_CERT_FILE", str(_TLS / "cert.pem"))

from fixture_server import STATS, FixtureServer, payload

from app.core import downloader as new_dl


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ORIG = load_module("downloader_original",
                   str(_HERE / "_baseline" / "downloader_original.py"))

# 计时场景的公共参数（对原版有利：把它的读空闲超时从 20s 放宽到 3s）
FAIR = {"read_idle_timeout": 3, "connect_timeout": 4,
        "retry_backoff_ms": 20, "backoff_max_ms": 60}

ROWS = []


def say(msg):
    print(msg, flush=True)


def row(name, orig, new, note=""):
    ROWS.append((name, orig, new, note))
    say(f"  . {name}: {orig}  ->  {new}   {note}")


def use(impl, **overrides):
    opts = dict(impl._DEFAULTS)
    opts.update(overrides)
    impl._OPT_CACHE = opts
    return opts


def task(impl, rel, urls, data=None, size=0, sha1=None, sha256=""):
    if data is not None:
        size = len(data)
        if sha1 is None and not sha256:
            sha1 = hashlib.sha1(data).hexdigest()
    return impl.DownloadTask(rel_path=rel, urls=list(urls),
                             sha1=sha1 or "", sha256=sha256,
                             file_size=size)


def verify(target: Path, data: bytes) -> bool:
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest() == \
            hashlib.sha256(data).hexdigest()
    except OSError:
        return False


def count_ok(tmp: Path, tasks, contents) -> int:
    """按磁盘实际内容判定成功数（原版只回失败项，不能只看返回值）。"""
    return sum(1 for t in tasks
               if contents.get(t.rel_path) is not None
               and verify(tmp / t.rel_path, contents[t.rel_path]))


def run_once(impl, tasks, tmp: Path, threads=None, contents=None, **opts):
    use(impl, **opts)
    STATS.reset()
    t0 = time.perf_counter()
    impl.download_files(tasks, target_dir=tmp, threads=threads)
    elapsed = time.perf_counter() - t0
    return {"elapsed": elapsed, "ok": count_ok(tmp, tasks, contents or {}),
            "conns": STATS.connections,
            "requests": sum(STATS.requests.values()),
            "max_conc": STATS.max_concurrent}


def median(impl, tasks, threads=None, reps=1, contents=None, **opts):
    times, ok, last = [], 0, None
    for _ in range(reps):
        with tempfile.TemporaryDirectory(prefix="bench-") as d:
            last = run_once(impl, tasks, Path(d), threads=threads,
                            contents=contents, **opts)
        times.append(last["elapsed"])
        ok = max(ok, last["ok"])
    assert last is not None
    last["ok"] = ok
    last["median"] = statistics.median(times)
    return last


def build_files(impl, srv, n, size, prefix, name_of):
    tasks, contents = [], {}
    for i in range(n):
        key = name_of(i)
        data = payload(key, size)
        rel = f"{prefix}/{key}"
        contents[rel] = data
        tasks.append(task(impl, rel, [srv.url("data", key, size)], data=data))
    return tasks, contents


# ======================================================================
def bench_http_small(srv):
    n, size, reps = 12, 24 * 1024, 2
    ot, oc = build_files(ORIG, srv, n, size, "mods", lambda i: f"h{i}.jar")
    nt, nc = build_files(new_dl, srv, n, size, "mods", lambda i: f"h{i}.jar")
    o = median(ORIG, ot, threads=8, reps=reps, contents=oc,
               multi_slots=8, single_slots=2, **FAIR)
    w = median(new_dl, nt, threads=8, reps=reps, contents=nc,
               multi_slots=8, single_slots=2, **FAIR)
    row(f"HTTP {n}x{size // 1024}KB（8 槽位）",
        f"{o['median']:.3f}s（连接{o['conns']}）",
        f"{w['median']:.3f}s（连接{w['conns']}）",
        f"加速 {o['median'] / max(w['median'], 1e-9):.1f}x  "
        f"均成功 {o['ok']}/{w['ok']}")


def bench_defaults(srv):
    """原版**默认参数**（read_idle 20s）下每文件的真实代价。"""
    n, size = 3, 8 * 1024
    ot, oc = build_files(ORIG, srv, n, size, "dflt", lambda i: f"d{i}.bin")
    nt, nc = build_files(new_dl, srv, n, size, "dflt", lambda i: f"d{i}.bin")
    o = median(ORIG, ot, threads=1, reps=1, contents=oc,
               multi_slots=1, single_slots=1)
    w = median(new_dl, nt, threads=1, reps=1, contents=nc,
               multi_slots=1, single_slots=1)
    row(f"HTTP {n} 个文件，双方都用**出厂默认**参数",
        f"{o['median']:.2f}s（{o['median'] / n:.2f}s/文件）",
        f"{w['median']:.3f}s（{w['median'] / n:.3f}s/文件）",
        f"加速 {o['median'] / max(w['median'], 1e-9):.0f}x  "
        "（原版默认 read_idle=20s）")


def bench_https_small(srv):
    n, size, reps = 8, 8 * 1024, 2
    ot, oc = build_files(ORIG, srv, n, size, "tls", lambda i: f"s{i}.bin")
    nt, nc = build_files(new_dl, srv, n, size, "tls", lambda i: f"s{i}.bin")
    o = median(ORIG, ot, threads=8, reps=reps, contents=oc,
               multi_slots=8, single_slots=2, **FAIR)
    w = median(new_dl, nt, threads=8, reps=reps, contents=nc,
               multi_slots=8, single_slots=2, **FAIR)
    row(f"HTTPS {n}x{size // 1024}KB（8 槽位，TLS 握手）",
        f"{o['median']:.3f}s（连接{o['conns']}）",
        f"{w['median']:.3f}s（连接{w['conns']}）",
        f"加速 {o['median'] / max(w['median'], 1e-9):.1f}x"
        "（含共享 SSLContext）")


def bench_https_many(srv):
    n, size, reps = 24, 8 * 1024, 1
    ot, oc = build_files(ORIG, srv, n, size, "tls2", lambda i: f"t{i}.bin")
    nt, nc = build_files(new_dl, srv, n, size, "tls2", lambda i: f"t{i}.bin")
    o = median(ORIG, ot, threads=8, reps=reps, contents=oc,
               multi_slots=8, single_slots=2, **FAIR)
    w = median(new_dl, nt, threads=8, reps=reps, contents=nc,
               multi_slots=8, single_slots=2, **FAIR)
    row(f"HTTPS {n}x{size // 1024}KB（8 槽位，多轮）",
        f"{o['median']:.3f}s（连接{o['conns']}）",
        f"{w['median']:.3f}s（连接{w['conns']}）",
        f"加速 {o['median'] / max(w['median'], 1e-9):.1f}x")


def bench_url_fallback(srv):
    n, size = 10, 16 * 1024

    def build(impl):
        tasks, contents = [], {}
        for i in range(n):
            data = payload(f"f{i}", size)
            rel = f"fb/f{i}.jar"
            contents[rel] = data
            tasks.append(task(impl, rel, [
                srv.url("missing", f"f{i}", size),
                srv.url("data", f"f{i}", size)], data=data))
        return tasks, contents
    ot, oc = build(ORIG)
    nt, nc = build(new_dl)
    o = median(ORIG, ot, threads=4, reps=1, contents=oc,
               multi_slots=4, single_slots=2, **FAIR)
    w = median(new_dl, nt, threads=4, reps=1, contents=nc,
               multi_slots=4, single_slots=2, **FAIR)
    row("换源：10 个文件 [404, 正常源]",
        f"成功 {o['ok']}/10", f"成功 {w['ok']}/10",
        "原版第 2 个源从不被尝试（G1）")


def bench_preallocated_part(srv):
    """预分配（全零）.part + 任务没有哈希：原版会把它当成品。"""
    size = 256 * 1024
    data = payload("hole", size)
    url = srv.url("data", "hole", size)

    def measure(impl):
        with tempfile.TemporaryDirectory(prefix="bench-") as d:
            tmp = Path(d)
            target = tmp / "hole.bin"
            part = target.with_suffix(target.suffix + ".part")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as f:
                f.truncate(size)          # 模拟分片预分配出来的零填充文件
            t = impl.DownloadTask(rel_path="hole.bin", urls=[url],
                                  sha1="", file_size=size)
            use(impl, multi_slots=1, single_slots=1, **FAIR)
            impl.download_files([t], target_dir=tmp, threads=1)
            if not target.is_file():
                return "文件不存在"
            blob = target.read_bytes()
            if blob == b"\x00" * size:
                return "X 零填充被判为下载成功"
            if verify(target, data):
                return "O 内容正确"
            return "X 内容错误"
    row("预分配零填充 .part（任务无哈希）",
        measure(ORIG), measure(new_dl), "数据损坏回归（G4）")


def bench_404_body(srv):
    def measure(impl):
        with tempfile.TemporaryDirectory(prefix="bench-") as d:
            tmp = Path(d)
            t = impl.DownloadTask(rel_path="gone.bin",
                                  urls=[srv.url("missing", "gone")],
                                  sha1="", file_size=0)
            use(impl, multi_slots=1, single_slots=1, **FAIR)
            impl.download_files([t], target_dir=tmp, threads=1)
            target = tmp / "gone.bin"
            if not target.is_file():
                return "O 未落盘"
            return (f"X 落盘 {target.stat().st_size}B："
                    f"{target.read_bytes()[:28]!r}")
    row("404 错误页处理", measure(ORIG), measure(new_dl), "G2")


def bench_chunked(srv):
    size = 32 * 1024
    data = payload("ck", size)

    def measure(impl):
        with tempfile.TemporaryDirectory(prefix="bench-") as d:
            tmp = Path(d)
            t = impl.DownloadTask(
                rel_path="ck.bin", urls=[srv.url("chunked", "ck", size)],
                sha256=hashlib.sha256(data).hexdigest(), file_size=0)
            use(impl, multi_slots=1, single_slots=1, retry_same_url=0,
                single_stall_timeout=3, single_url_timeout=8, **FAIR)
            t0 = time.perf_counter()
            impl.download_files([t], target_dir=tmp, threads=1)
            dt = time.perf_counter() - t0
            p = tmp / "ck.bin"
            if p.is_file() and verify(p, data):
                return f"O 成功（{dt:.1f}s）"
            return f"X 失败（{dt:.1f}s，读空闲误判）"
    row("未知大小（chunked，无 Content-Length）",
        measure(ORIG), measure(new_dl), "G5")


def bench_big_file(srv):
    """8 MiB 文件：分片直写 + 收尾改名，没有拼接。"""
    size = 8 * 1024 * 1024
    data = payload("big", size)
    url = srv.url("data", "big", size)

    def measure(impl):
        with tempfile.TemporaryDirectory(prefix="bench-") as d:
            tmp = Path(d)
            t = impl.DownloadTask(
                rel_path="big.bin", urls=[url],
                sha256=hashlib.sha256(data).hexdigest(), file_size=size)
            use(impl, multi_slots=1, single_slots=1, part_threads=4, **FAIR)
            t0 = time.perf_counter()
            impl.download_files([t], target_dir=tmp, threads=1)
            dt = time.perf_counter() - t0
            p = tmp / "big.bin"
            residue = sorted(x.name for x in tmp.rglob("*.part*"))
            middle = sorted(x.name for x in tmp.rglob("*.part.[0-9]*"))
            okk = "O" if (p.is_file() and verify(p, data)) else "X"
            return (f"{okk} {dt:.2f}s 残片={residue or '无'} "
                    f"中间分片={middle or '无'}")
    row("8 MiB 单文件（分片直写 + rename 收尾）",
        measure(ORIG), measure(new_dl), "无拼接路径")


def bench_keepalive(srv):
    n, size = 12, 8 * 1024
    ot, oc = build_files(ORIG, srv, n, size, "ka", lambda i: f"k{i}.bin")
    nt, nc = build_files(new_dl, srv, n, size, "ka", lambda i: f"k{i}.bin")
    o = median(ORIG, ot, threads=1, reps=1, contents=oc,
               multi_slots=1, single_slots=1, **FAIR)
    w = median(new_dl, nt, threads=1, reps=1, contents=nc,
               multi_slots=1, single_slots=1, **FAIR)
    row("12 个文件顺序下载（keep-alive 复用）",
        f"{o['median']:.2f}s 连接{o['conns']} 请求{o['requests']}",
        f"{w['median']:.2f}s 连接{w['conns']} 请求{w['requests']}",
        f"加速 {o['median'] / max(w['median'], 1e-9):.0f}x")


def main():
    http = FixtureServer().start()
    https = None
    if (_TLS / "cert.pem").is_file():
        https = FixtureServer(certfile=str(_TLS / "cert.pem"),
                              keyfile=str(_TLS / "key.pem")).start()
    say("=" * 78)
    say("原版 vs 改进版 —— 本地模拟源成对 A/B")
    say(f"HTTP 源 {http.base}")
    say(f"HTTPS 源 {https.base if https else '（未启用）'}")
    say("=" * 78)
    t_all = time.perf_counter()
    try:
        warm = FixtureServer().start()
        for i in range(3):
            median(new_dl, [task(new_dl, f"w{i}.bin",
                                 [warm.url("data", f"w{i}", 4096)],
                                 data=payload(f"w{i}", 4096))],
                   threads=1, reps=1)
        warm.stop()

        say("\n[1/9] HTTP 12x24KB ...")
        bench_http_small(http)
        say("[2/9] 默认参数下的单文件代价 ...")
        bench_defaults(http)
        if https is not None:
            say("[3/9] HTTPS 8x8KB ...")
            bench_https_small(https)
            say("[4/9] HTTPS 24x8KB ...")
            bench_https_many(https)
        say("[5/9] 多源回退 ...")
        bench_url_fallback(http)
        say("[6/9] 预分配 .part ...")
        bench_preallocated_part(http)
        say("[7/9] 404 错误页 ...")
        bench_404_body(http)
        say("[8/9] chunked 未知大小 ...")
        bench_chunked(http)
        say("[9/9] 大文件 / 连接复用 ...")
        bench_big_file(http)
        bench_keepalive(http)
    finally:
        http.stop()
        if https is not None:
            https.stop()

    say("\n" + "=" * 78)
    say(f"汇总（总耗时 {time.perf_counter() - t_all:.0f}s）")
    say("=" * 78)
    for name, a, b, note in ROWS:
        say(f"\n# {name}")
        say(f"    原版  : {a}")
        say(f"    改进版: {b}")
        if note:
            say(f"    说明  : {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

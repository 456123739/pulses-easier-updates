"""
test_downloader.py — 下载器功能测试（本地 simulated 源，不依赖真实网络）。

覆盖用户要求验证的四项：
  1. 下载器模块能正常初始化（配置读写、空任务、异常兜底）
  2. 多线程下载逻辑能正常调度（并发度、单文件分片、连接复用）
  3. 降级 / 换源 / 超时策略在模拟条件下能正常触发
  4. 进度上报不阻塞、单调、不越界（回调形态与调用方 player_view 对齐）

另外把本轮修的 4 个正确性缺陷做成回归用例：
  G1 多源回退失效 / G2 状态码不检查 / G3 Range 不校验 / G4 预分配 .part 被当成成品
以及 G5 读空闲误判（chunked 未知大小）。

运行：  python3 tests/test_downloader.py
"""

import hashlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from fixture_server import STATS, FixtureServer, payload

from app.core import downloader as dl


def set_opts(**kw):
    """直接注入配置（绕过数据库），模拟用户在首选项里改了参数。"""
    opts = dict(dl._DEFAULTS)
    opts.update(kw)
    dl._OPT_CACHE = opts
    return opts


def opts_now():
    return dl._get_options()


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FixtureServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        STATS.reset()
        set_opts()
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-dl-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        dl.invalidate_options()

    # -- helpers -------------------------------------------------------
    def task(self, rel, urls, data=None, size=0, sha1=None, sha256=None):
        if data is not None:
            size = len(data)
        return dl.DownloadTask(
            rel_path=rel, urls=list(urls),
            sha1=sha1 if sha1 is not None else
            (sha1_of(data) if data is not None else ""),
            sha256=sha256 or "",
            file_size=size)

    def run_dl(self, tasks, **kw):
        return dl.download_files(tasks, target_dir=self.tmp, **kw)

    def assert_content(self, rel, expected: bytes):
        p = self.tmp / rel
        self.assertTrue(p.is_file(), f"{rel} 不存在")
        self.assertEqual(p.stat().st_size, len(expected),
                         f"{rel} 大小不符")
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),
                         hashlib.sha256(expected).hexdigest(),
                         f"{rel} 内容不符")

    def assert_no_part(self, rel):
        target = self.tmp / rel
        part = target.with_suffix(target.suffix + ".part")
        self.assertFalse(part.exists(), f"{part.name} 未清理")
        self.assertFalse(part.with_name(part.name + ".meta").exists(),
                         f"{part.name}.meta 未清理")


# ======================================================================
# 1. 初始化
# ======================================================================
class TestInit(TestBase):
    def test_defaults_present(self):
        opts = opts_now()
        for key in ("multi_slots", "single_slots", "part_threads",
                    "max_connections", "read_poll_interval",
                    "http_status_check", "range_validate",
                    "part_meta_enabled", "hash_while_streaming"):
            self.assertIn(key, opts, f"缺少配置项 {key}")
        self.assertEqual(opts["multi_slots"], 12)
        self.assertEqual(opts["read_poll_interval"], 1.0)
        self.assertEqual(opts["http_status_check"], 1)

    def test_invalidate_options(self):
        set_opts()
        dl.invalidate_options()
        # 无数据库环境下应安全回落到内置默认值
        opts = opts_now()
        self.assertIsInstance(opts, dict)
        self.assertIn("multi_slots", opts)

    def test_empty_task_list(self):
        seen = []
        out = self.run_dl([], progress=lambda d, t, n: seen.append((d, t, n)))
        self.assertEqual(out, [])
        self.assertEqual(seen[-1], (0, 0, ""))

    def test_public_api_shape(self):
        import inspect
        sig = inspect.signature(dl.download_files)
        self.assertEqual(
            list(sig.parameters),
            ["tasks", "target_dir", "threads", "progress", "log",
             "byte_progress", "should_abort", "on_file_started",
             "on_file_done", "on_file_failed"])
        names = [f.name for f in dl.DownloadTask.__dataclass_fields__.values()]
        self.assertEqual(names, ["rel_path", "urls", "sha1", "sha256",
                                 "sha512", "file_size"])
        names = [f.name for f in dl.DownloadResult.__dataclass_fields__.values()]
        self.assertEqual(names, ["task", "ok", "target", "error", "aborted"])
        self.assertTrue(callable(dl.estimate_eta))
        self.assertTrue(callable(dl.invalidate_options))

    def test_estimate_eta(self):
        self.assertEqual(dl.estimate_eta(0, 100, time.time()), 0)
        self.assertTrue(dl.estimate_eta(50, 100, time.time() - 1) >= 0)


# ======================================================================
# 2. 多线程调度
# ======================================================================
class TestScheduling(TestBase):
    def test_parallel_slots_and_content(self):
        set_opts(multi_slots=8, single_slots=2)
        n = 40
        size = 24 * 1024
        tasks, expect = [], {}
        for i in range(n):
            rel = f"mods/mod_{i}.jar"
            data = payload(f"m{i}", size)
            expect[rel] = data
            tasks.append(self.task(rel, [self.srv.url("data", f"m{i}", size)],
                                   data=data))
        done = []
        results = self.run_dl(tasks, progress=lambda d, t, x: done.append(d))
        self.assertEqual(len(results), n)
        self.assertTrue(all(r.ok for r in results),
                        [r.error for r in results if not r.ok])
        for rel, data in expect.items():
            self.assert_content(rel, data)
        self.assertGreater(STATS.max_concurrent, 1, "没有并发起来")
        self.assertLessEqual(STATS.max_concurrent, 8)
        self.assertEqual(done[-1], n)

    def test_subdirectory_creation(self):
        data = payload("deep", 4096)
        rel = "config/a/b/c/settings.json"
        res = self.run_dl([self.task(rel, [self.srv.url("data", "deep", 4096)],
                                     data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content(rel, data)

    def test_connection_reuse(self):
        set_opts(multi_slots=1, single_slots=1)
        tasks = []
        for i in range(12):
            data = payload(f"ka{i}", 8192)
            tasks.append(self.task(f"ka{i}.bin",
                                   [self.srv.url("data", f"ka{i}", 8192)],
                                   data=data))
        res = self.run_dl(tasks, threads=1)
        self.assertTrue(all(r.ok for r in res))
        self.assertLess(STATS.connections, 12,
                        f"keep-alive 没生效（连接数 {STATS.connections}）")

    def test_redirect_followed(self):
        data = payload("rd", 8192)
        res = self.run_dl([self.task("rd.bin",
                                     [self.srv.url("redirect", "rd", 8192)],
                                     data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("rd.bin", data)


# ======================================================================
# 3. 换源 / 降级 / 超时
# ======================================================================
class TestFallback(TestBase):
    def test_url_fallback_second_source_used(self):
        """G1 回归：第 2 个 URL 必须真的被尝试。"""
        data = payload("fb", 8192)
        good = self.srv.url("data", "fb", 8192)
        bad = self.srv.url("missing", "fb", 8192)
        res = self.run_dl([self.task("fb.bin", [bad, good], data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("fb.bin", data)
        self.assertGreaterEqual(STATS.count("/missing/fb"), 1)
        self.assertGreaterEqual(STATS.count("/data/fb"), 1,
                                "第 2 个源根本没被请求（G1 回归）")

    def test_url_fallback_three_sources(self):
        data = payload("fb3", 8192)
        res = self.run_dl([self.task("fb3.bin", [
            self.srv.url("boom", "fb3", 8192),
            self.srv.url("missing", "fb3", 8192),
            self.srv.url("data", "fb3", 8192)], data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("fb3.bin", data)
        self.assertGreaterEqual(STATS.count("/boom/fb3"), 1)
        self.assertGreaterEqual(STATS.count("/data/fb3"), 1)

    def test_stall_source_switches(self):
        """慢源触发 stall → 换到正常源。"""
        set_opts(stall_timeout=2, per_url_timeout=6,
                 single_stall_timeout=3, single_url_timeout=8,
                 retry_backoff_ms=50, speed_window=2.0,
                 min_speed_bps=1024, multi_slots=2)
        data = payload("st", 64 * 1024)
        res = self.run_dl([self.task("st.bin", [
            self.srv.url("stall", "st", 64 * 1024),
            self.srv.url("data", "st", 64 * 1024)], data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("st.bin", data)
        self.assertGreaterEqual(STATS.count("/stall/st"), 1, "慢源未被尝试")
        self.assertGreaterEqual(STATS.count("/data/st"), 1, "没有换源")

    def test_all_sources_stall_fails_bounded(self):
        set_opts(stall_timeout=1, per_url_timeout=3,
                 single_stall_timeout=1, single_url_timeout=3,
                 retry_backoff_ms=30, retry_same_url=0,
                 speed_window=1.0, min_speed_bps=1,
                 multi_slots=1, single_slots=1)
        data = payload("stf", 64 * 1024)
        t0 = time.time()
        res = self.run_dl([self.task("stf.bin", [
            self.srv.url("stall", "stf", 64 * 1024)], data=data)])
        elapsed = time.time() - t0
        self.assertFalse(res[0].ok)
        self.assertLess(elapsed, 20, f"超时策略没生效，耗时 {elapsed:.1f}s")
        self.assertFalse((self.tmp / "stf.bin").exists())

    def test_http_404_not_saved(self):
        """G2 回归：404 错误页不能落盘。"""
        res = self.run_dl([self.task(
            "gone.bin", [self.srv.url("missing", "gone", 4096)],
            size=4096, sha1="00" * 20)])
        self.assertFalse(res[0].ok)
        self.assertFalse((self.tmp / "gone.bin").exists(), "404 正文被写盘")
        self.assertIn("404", res[0].error)

    def test_retryable_status_retried(self):
        import fixture_server
        fixture_server.FLAKY_FAILS = 2
        set_opts(retry_same_url=2, retryable_status_retry=2,
                 retry_backoff_ms=20, backoff_max_ms=50,
                 multi_slots=1, single_slots=1)
        data = payload("fl", 8192)
        res = self.run_dl([self.task(
            "fl.bin", [self.srv.url("flaky", "fl", 8192)], data=data)])
        fixture_server.FLAKY_FAILS = 0
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("fl.bin", data)

    def test_truncated_body_fails(self):
        set_opts(multi_slots=1, single_slots=1, retry_same_url=0,
                 single_stall_timeout=2, single_url_timeout=4,
                 stall_timeout=2, per_url_timeout=4,
                 retry_backoff_ms=20, backoff_max_ms=50,
                 min_speed_bps=1, single_min_speed_bps=1,
                 speed_window=1.0)
        res = self.run_dl([self.task(
            "tr.bin", [self.srv.url("truncate", "tr", 64 * 1024)],
            size=64 * 1024, sha1="11" * 20)])
        self.assertFalse(res[0].ok, "截断的响应被当成成功")
        self.assertFalse((self.tmp / "tr.bin").exists())

    def test_unknown_size_chunked(self):
        """G5 回归：无 Content-Length（chunked）也能下完。"""
        set_opts(multi_slots=1, single_slots=1, retry_same_url=0)
        data = payload("ck", 32 * 1024)
        task = dl.DownloadTask(
            rel_path="ck.bin",
            urls=[self.srv.url("chunked", "ck", 32 * 1024)],
            sha256=sha256_of(data), file_size=0)
        res = self.run_dl([task])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("ck.bin", data)


# ======================================================================
# 4. 单文件分片 / Range 校验 / 续传
# ======================================================================
class TestParts(TestBase):
    def _ctx(self):
        return None

    def test_multipart_writes_correct_data(self):
        total = 6 * 1024 * 1024
        data = payload("big", total)
        url = self.srv.url("data", "big", total)
        task = dl.DownloadTask("big.bin", [url],
                               sha256=sha256_of(data), file_size=total)
        target = self.tmp / "big.bin"
        res = dl._download_multi_part(url, target, task, total, None,
                                      lambda: False, opts_now())
        self.assertTrue(res.ok, res.error)
        self.assert_content("big.bin", data)
        self.assert_no_part("big.bin")
        ranges = [r for r in STATS.ranges if r]
        self.assertGreaterEqual(len(ranges), 2, "没有发出分片请求")

    def test_range_ignored_is_rejected(self):
        """G3 回归：源忽略 Range 时必须拒绝，不能写出错位文件。"""
        total = 5 * 1024 * 1024
        data = payload("nr", total)
        url = self.srv.url("norange", "nr", total)
        task = dl.DownloadTask("nr.bin", [url],
                               sha256=sha256_of(data), file_size=total)
        target = self.tmp / "nr.bin"
        res = dl._download_multi_part(url, target, task, total, None,
                                      lambda: False, opts_now())
        self.assertFalse(res.ok)
        self.assertTrue(res.range_unsupported, "没有识别出源不支持分段")
        self.assertFalse(target.exists(), "产生了错位文件")
        self.assert_no_part("nr.bin")

    def test_range_ignored_then_fallback_source(self):
        """忽略 Range 的源 + 正常源 → 必须最终成功且内容正确。"""
        total = 5 * 1024 * 1024
        data = payload("nr2", total)
        res = self.run_dl([self.task("nr2.bin", [
            self.srv.url("norange", "nr2", total),
            self.srv.url("data", "nr2", total)], data=data)])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("nr2.bin", data)

    def test_resume_uses_range(self):
        total = 512 * 1024
        data = payload("resume", total)
        url = self.srv.url("data", "resume", total)
        target = self.tmp / "resume.bin"
        part = target.with_suffix(target.suffix + ".part")
        head = total // 2
        part.write_bytes(data[:head])
        dl._save_ranges(part, [(0, head - 1)], total)
        task = dl.DownloadTask("resume.bin", [url],
                               sha256=sha256_of(data), file_size=total)
        res = dl._try_single_url(url, target, part, task, None,
                                 lambda: False, opts_now())
        self.assertTrue(res.ok, res.error)
        self.assert_content("resume.bin", data)
        self.assertTrue(any(r.startswith(f"bytes={head}-")
                            for r in STATS.ranges),
                        f"没有按断点发 Range：{STATS.ranges}")

    def test_preallocated_part_not_treated_as_complete(self):
        """G4 回归：预分配（全零）的 .part 不能被当成下载完成。"""
        data = payload("hole", 256 * 1024)
        url = self.srv.url("data", "hole", len(data))
        target = self.tmp / "hole.bin"
        part = target.with_suffix(target.suffix + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "wb") as f:          # 模拟分片预分配：长度=最终大小
            f.truncate(len(data))
        task = self.task("hole.bin", [url], data=data)
        outcome = dl._attempt_file(task, self.tmp, None, lambda: False,
                                   opts_now())
        self.assertTrue(outcome.ok, outcome.error)
        blob = target.read_bytes()
        self.assertNotEqual(blob, b"\x00" * len(data),
                            "零填充的 .part 被当成成品了（G4 回归）")
        self.assert_content("hole.bin", data)

    def test_part_with_meta_full_range_is_accepted(self):
        """sidecar 明确记录全覆盖时，应当直接收尾（不重下）。"""
        data = payload("done", 64 * 1024)
        url = self.srv.url("data", "done", len(data))
        target = self.tmp / "done.bin"
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(data)
        dl._save_ranges(part, [(0, len(data) - 1)], len(data))
        task = self.task("done.bin", [url], data=data)
        res = dl._try_single_url(url, target, part, task, None,
                                 lambda: False, opts_now())
        self.assertTrue(res.ok)
        self.assert_content("done.bin", data)
        self.assertEqual(STATS.count("/data/done"), 0,
                         "已有完整 .part 却仍然发起了请求")


# ======================================================================
# 5. 哈希与进度
# ======================================================================
class TestHashAndProgress(TestBase):
    def test_streaming_hash_ok(self):
        data = payload("h1", 128 * 1024)
        res = self.run_dl([dl.DownloadTask(
            "h1.bin", [self.srv.url("data", "h1", len(data))],
            sha256=sha256_of(data), file_size=len(data))])
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("h1.bin", data)

    def test_streaming_hash_mismatch_rejected(self):
        data = payload("h2", 128 * 1024)
        set_opts(retry_same_url=0, retryable_status_retry=0)
        res = self.run_dl([dl.DownloadTask(
            "h2.bin", [self.srv.url("data", "h2", len(data))],
            sha256="ab" * 32, file_size=len(data))])
        self.assertFalse(res[0].ok)
        self.assertFalse((self.tmp / "h2.bin").exists(),
                         "校验失败的文件被留下了")

    def test_byte_progress_monotonic(self):
        total = 8 * 1024 * 1024
        data = payload("pg", total)
        seen = {}

        def on_bytes(rel, received, tot):
            seen.setdefault(rel, []).append(received)

        res = self.run_dl([dl.DownloadTask(
            "pg.bin", [self.srv.url("data", "pg", total)],
            sha256=sha256_of(data), file_size=total)],
            byte_progress=on_bytes)
        self.assertTrue(res[0].ok, res[0].error)
        seq = seen.get("pg.bin", [])
        self.assertTrue(seq, "没有收到分片进度回调")
        self.assertEqual(seq, sorted(seq), f"进度回退：{seq[:12]}")
        self.assertLessEqual(max(seq), total)
        self.assertEqual(seq[-1], total, "最终进度不等于文件大小")

    def test_file_progress_never_exceeds_total(self):
        set_opts(multi_slots=4, single_slots=2, retry_backoff_ms=20,
                 stall_timeout=2, per_url_timeout=4,
                 single_stall_timeout=2, single_url_timeout=4,
                 speed_window=2.0, min_speed_bps=1024,
                 single_min_speed_bps=1024)
        tasks = []
        for i in range(6):
            data = payload(f"mix{i}", 8192)
            urls = [self.srv.url("data", f"mix{i}", 8192)]
            if i % 2:
                urls = [self.srv.url("missing", f"mix{i}", 8192)] + urls
            tasks.append(self.task(f"mix{i}.bin", urls, data=data))
        seen = []
        results = self.run_dl(
            tasks, progress=lambda d, t, n: seen.append((d, t)))
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r.ok for r in results),
                        [r.error for r in results if not r.ok])
        self.assertTrue(all(d <= t for d, t in seen), f"done 超过 total：{seen}")
        self.assertEqual(seen[-1][0], 6)

    def test_callbacks_order_and_failure_only_final(self):
        set_opts(multi_slots=2, single_slots=1, retry_backoff_ms=20)
        good = payload("cb", 8192)
        started, done, failed = [], [], []
        tasks = [
            self.task("cb_ok.bin", [self.srv.url("data", "cb", 8192)],
                      data=good),
            self.task("cb_bad.bin", [self.srv.url("missing", "cb", 8192)],
                      size=8192, sha1="22" * 20),
        ]
        res = self.run_dl(
            tasks,
            on_file_started=lambda t: started.append(t.rel_path),
            on_file_done=lambda t: done.append(t.rel_path),
            on_file_failed=lambda t, e: failed.append(t.rel_path))
        # v0.5.0：on_file_done 只在**成功**时触发（调用方靠它统计"已完成"），
        # 失败与中止只走 on_file_failed
        self.assertEqual(done, ["cb_ok.bin"],
                         "on_file_done 应当只在成功时触发一次")
        self.assertEqual(failed, ["cb_bad.bin"])
        # on_file_started 允许重试时重复触发（多线程池 → 耐心池）
        self.assertEqual(set(started), {"cb_bad.bin", "cb_ok.bin"})
        self.assertEqual(len(res), 2)
        self.assertEqual(sum(1 for r in res if r.ok), 1)

    def test_results_one_entry_per_task(self):
        tasks = []
        for i in range(5):
            data = payload(f"rp{i}", 4096)
            urls = [self.srv.url("data", f"rp{i}", 4096)]
            if i == 3:
                urls = [self.srv.url("missing", f"rp{i}", 4096)]
            tasks.append(self.task(f"rp{i}.bin", urls, data=data))
        res = self.run_dl(tasks)
        self.assertEqual(len(res), len(tasks),
                         "结果条数必须与任务数一致（调用方按此统计成功数）")
        self.assertEqual(sum(1 for r in res if r.ok), 4)

    def test_caller_arithmetic_matches_player_view(self):
        """
        复刻 player_view 的统计口径：
            failed = [r for r in results if not r.ok and not r.aborted]
            成功 = len(results) - len(failed)
            completed = [r.task.rel_path for r in results if r.ok]
        """
        tasks = []
        for i in range(6):
            data = payload(f"pa{i}", 4096)
            urls = [self.srv.url("data", f"pa{i}", 4096)]
            if i in (2, 5):
                urls = [self.srv.url("missing", f"pa{i}", 4096)]
            tasks.append(self.task(f"pa{i}.bin", urls, data=data))
        results = self.run_dl(tasks,
                              on_file_failed=lambda t, e: None)
        failed = [r for r in results if not r.ok and not r.aborted]
        ok_list = [r for r in results if r.ok]
        self.assertEqual(len(results) - len(failed), 4,
                         "「成功 N」算错（口径见 player_view）")
        self.assertEqual(len(ok_list), 4)
        completed = [r.task.rel_path for r in ok_list]
        self.assertEqual(sorted(completed),
                         ["pa0.bin", "pa1.bin", "pa3.bin", "pa4.bin"])
        self.assertEqual(sorted(r.task.rel_path for r in failed),
                         ["pa2.bin", "pa5.bin"])


# ======================================================================
# 6. 中止
# ======================================================================
class TestAbort(TestBase):
    def test_abort_stops_quickly(self):
        total = 8 * 1024 * 1024
        data = payload("ab", total)
        flag = {"v": False}
        threading.Timer(0.35, lambda: flag.update(v=True)).start()
        t0 = time.time()
        res = self.run_dl([dl.DownloadTask(
            "ab.bin", [self.srv.url("slow", "ab", total)],
            sha256=sha256_of(data), file_size=total)],
            should_abort=lambda: flag["v"])
        elapsed = time.time() - t0
        self.assertTrue(res and res[0].aborted, f"未记为中止：{res}")
        self.assertLess(elapsed, 6, f"中止响应太慢：{elapsed:.1f}s")
        self.assertFalse((self.tmp / "ab.bin").exists())

    def test_abort_returns_all_tasks(self):
        flag = {"v": False}
        tasks = []
        for i in range(8):
            data = payload(f"a{i}", 64 * 1024)
            tasks.append(self.task(f"a{i}.bin",
                                   [self.srv.url("slow", f"a{i}", 64 * 1024)],
                                   data=data))
        threading.Timer(0.3, lambda: flag.update(v=True)).start()
        res = self.run_dl(tasks, threads=2,
                          should_abort=lambda: flag["v"])
        self.assertEqual(len(res), len(tasks))
        self.assertTrue(any(r.aborted for r in res))


# ======================================================================
# 7. 按磁盘类型限制并发（G11）
# ======================================================================
class TestDiskPolicy(TestBase):
    def setUp(self):
        super().setUp()
        dl._DISK_CACHE.clear()

    def test_classify_fs(self):
        for fs in ("nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.rclone",
                   "9p", "davfs"):
            self.assertEqual(dl._classify_fs(fs), "network", fs)
        self.assertEqual(dl._classify_fs("iso9660"), "removable")
        self.assertEqual(dl._classify_fs("udf"), "removable")
        for fs in ("ext4", "xfs", "btrfs", "ntfs", "apfs", "tmpfs", ""):
            self.assertIsNone(dl._classify_fs(fs), fs)

    def test_detect_local_disk(self):
        info = dl.detect_disk_type(self.tmp)
        self.assertIn(info["type"], dl._DISK_TYPES)
        self.assertIn("source", info)
        # 同一路径必须命中缓存
        again = dl.detect_disk_type(self.tmp)
        self.assertTrue(again.get("cached"))
        self.assertEqual(again["type"], info["type"])

    def test_probe_failure_is_unknown(self):
        orig = dl._linux_mount_of
        dl._linux_mount_of = lambda path: None
        try:
            dl._DISK_CACHE.clear()
            info = dl.detect_disk_type(self.tmp)
            self.assertEqual(info["type"], "unknown")
            pol = dl.resolve_disk_policy(self.tmp, set_opts(
                disk_type_override="", multi_slots=16, part_threads=4))
            self.assertEqual(pol["multi_slots"], 16)
            self.assertEqual(pol["effective_part_threads"], 4)
        finally:
            dl._linux_mount_of = orig
            dl._DISK_CACHE.clear()

    def test_override_limits_are_downgrade_only(self):
        cases = {
            "hdd": (4, 1),
            "network": (6, 1),
            "removable": (2, 1),
            "ssd": (16, 4),
            "unknown": (16, 4),
        }
        for kind, (files, parts) in cases.items():
            pol = dl.resolve_disk_policy(self.tmp, set_opts(
                disk_type_override=kind, multi_slots=16, part_threads=4))
            self.assertEqual(pol["multi_slots"], files, kind)
            self.assertEqual(pol["effective_part_threads"], parts, kind)

        # 用户配置本来就低于上限 → 只降不升，不能反被抬高
        pol = dl.resolve_disk_policy(self.tmp, set_opts(
            disk_type_override="hdd", multi_slots=2, part_threads=1))
        self.assertEqual(pol["multi_slots"], 2)
        self.assertEqual(pol["effective_part_threads"], 1)

    def test_disabled_switch_has_no_effect(self):
        pol = dl.resolve_disk_policy(self.tmp, set_opts(
            disk_aware_slots=0, disk_type_override="hdd",
            multi_slots=16, part_threads=4))
        self.assertEqual(pol["multi_slots"], 16)
        self.assertEqual(pol["effective_part_threads"], 4)

    def _multipart_gate_probe(self, kind, part_threads):
        """
        探针：走完整的 download_files 入口（磁盘策略在这一层生效），
        看给定磁盘类型下会不会进入分片路径。
        用 404 源让单连接必然失败，从而走到「第 3 次：分片」那一支。
        """
        called = []
        orig = dl._download_multi_part

        def spy(*a, **k):
            called.append(1)
            return orig(*a, **k)

        set_opts(disk_type_override=kind, multi_slots=16,
                 part_threads=part_threads, retry_same_url=0,
                 retryable_status_retry=0, retry_backoff_ms=10,
                 backoff_max_ms=20, connect_timeout=3,
                 read_idle_timeout=3, stall_timeout=3,
                 single_stall_timeout=3, single_url_timeout=6)
        dl._DISK_CACHE.clear()
        dl._download_multi_part = spy
        try:
            total = 6 * 1024 * 1024
            task = dl.DownloadTask(
                "dead.bin", [self.srv.url("missing", "dead")],
                sha1="", file_size=total)
            results = dl.download_files([task], target_dir=self.tmp,
                                        threads=None)
        finally:
            dl._download_multi_part = orig
        return called, results

    def test_hdd_override_skips_multipart(self):
        """part_threads 被磁盘策略压到 1 → 根本不进入分片路径。"""
        called, results = self._multipart_gate_probe("hdd", 4)
        self.assertEqual(called, [], "机械盘策略下仍然进入了分片路径")
        self.assertFalse(results[0].ok)
        self.assertGreaterEqual(STATS.count("/missing/dead"), 1)

    def test_ssd_override_keeps_multipart(self):
        called, _results = self._multipart_gate_probe("ssd", 4)
        self.assertEqual(len(called), 1, "固态策略下分片路径被误关")

    def test_hdd_policy_downloads_big_file_sequentially(self):
        """机械盘策略下 6 MiB 文件必须完整、且全程不发 Range。"""
        total = 6 * 1024 * 1024
        data = payload("hddbig", total)
        logs = []

        def fake_log(level, msg):
            logs.append((level, msg))

        set_opts(disk_type_override="hdd", multi_slots=16, part_threads=4)
        dl._DISK_CACHE.clear()
        results = dl.download_files(
            [dl.DownloadTask("hddbig.bin",
                             [self.srv.url("data", "hddbig", total)],
                             sha256=sha256_of(data), file_size=total)],
            target_dir=self.tmp, threads=None, log=fake_log)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].error)
        self.assert_content("hddbig.bin", data)
        self.assertEqual([r for r in STATS.ranges if r], [],
                         f"机械盘策略下仍然发了分片请求：{STATS.ranges}")
        self.assertTrue(any("hdd" in m for _lv, m in logs),
                        f"没有把磁盘策略写进日志：{logs}")

    def test_summary_line(self):
        text = dl.disk_policy_summary(self.tmp, set_opts(
            disk_type_override="hdd", multi_slots=16, part_threads=4))
        self.assertIn("hdd", text)
        self.assertIn("16", text)
        self.assertIn("4", text)

    def test_probe_disk_cache_warms_and_skips_reprobe(self):
        """
        启动时探一次就该够：之后 resolve 只做字典命中，
        不再碰 /proc/mounts、sysfs（Windows 上则是 IOCTL / PowerShell）。
        """
        calls = {"n": 0}
        orig = dl._linux_mount_of

        def counting(path):
            calls["n"] += 1
            return orig(path)

        dl._linux_mount_of = counting
        try:
            dl.clear_disk_cache()
            info = dl.probe_disk_cache(self.tmp)
            self.assertIn(info["type"], dl._DISK_TYPES)
            self.assertTrue(info.get("probed_at_startup"))
            self.assertEqual(calls["n"], 1)

            for _ in range(5):
                dl.resolve_disk_policy(self.tmp, set_opts(
                    disk_type_override="", multi_slots=16, part_threads=4))
            self.assertEqual(calls["n"], 1,
                             f"启动探过之后又探测了 {calls['n'] - 1} 次")
        finally:
            dl._linux_mount_of = orig
            dl.clear_disk_cache()

    def test_clear_disk_cache_forces_reprobe(self):
        dl.clear_disk_cache()
        dl.probe_disk_cache(self.tmp)
        dl.clear_disk_cache()
        self.assertEqual(dl._DISK_CACHE, {})


# ======================================================================
# 8. 分片策略按大小分级（静态，不做动态拆分）
# ======================================================================
class TestSizeTiers(TestBase):
    def test_part_count_tiers(self):
        o = set_opts()
        cases = [(1, 1), (3, 1), (4, 2), (8, 2), (16, 2),
                 (20, 3), (64, 8), (100, 8), (1024, 8)]
        for mb, want in cases:
            got = dl._planned_part_count(mb * 1024 * 1024, o)
            self.assertEqual(got, want, f"{mb}MiB → {got}，期望 {want}")

    def test_part_count_respects_config(self):
        o = set_opts(target_part_size=4 * 1024 * 1024, max_part_count=4,
                     multi_part_min_bytes=2 * 1024 * 1024)
        self.assertEqual(dl._planned_part_count(1 * 1024 * 1024, o), 1)
        self.assertEqual(dl._planned_part_count(8 * 1024 * 1024, o), 2)
        self.assertEqual(dl._planned_part_count(64 * 1024 * 1024, o), 4)

    def test_actual_ranges_match_planned_count(self):
        o = set_opts()
        total = 20 * 1024 * 1024
        n = dl._planned_part_count(total, o)
        ranges = dl._part_ranges(total, n)
        self.assertEqual(len(ranges), n)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], total - 1)
        for i in range(len(ranges) - 1):
            self.assertEqual(ranges[i][1] + 1, ranges[i + 1][0],
                             "分片区间不连续")

    def test_small_file_never_multiparts(self):
        """< multi_part_min_bytes 的文件即使单连接失败也不该走分片。"""
        calls = []
        orig = dl._download_multi_part

        def spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        set_opts(multi_part_min_bytes=4 * 1024 * 1024, retry_same_url=0,
                 retryable_status_retry=0, retry_backoff_ms=10,
                 disk_aware_slots=0)
        dl._download_multi_part = spy
        try:
            task = dl.DownloadTask("small.bin",
                                   [self.srv.url("missing", "small")],
                                   sha1="", file_size=2 * 1024 * 1024)
            dl.download_files([task], target_dir=self.tmp, threads=1)
        finally:
            dl._download_multi_part = orig
        self.assertEqual(calls, [], "2MiB 文件不该进分片路径")

    def test_large_file_multi_first_when_enabled(self):
        """large_file_multi_first_bytes>0 时，超大文件应先试分片。"""
        total = 20 * 1024 * 1024
        data = payload("mfirst", total)
        order = []
        orig_multi = dl._download_multi_part
        orig_single = dl._try_single_url

        def spy_multi(*a, **k):
            order.append("multi")
            return orig_multi(*a, **k)

        def spy_single(*a, **k):
            order.append("single")
            return orig_single(*a, **k)

        dl._download_multi_part = spy_multi
        dl._try_single_url = spy_single
        try:
            set_opts(large_file_multi_first_bytes=16 * 1024 * 1024,
                     disk_aware_slots=0, part_threads=4)
            res = dl.download_files(
                [dl.DownloadTask("mfirst.bin",
                                 [self.srv.url("data", "mfirst", total)],
                                 sha256=sha256_of(data), file_size=total)],
                target_dir=self.tmp)
        finally:
            dl._download_multi_part = orig_multi
            dl._try_single_url = orig_single
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mfirst.bin", data)
        self.assertEqual(order[:1], ["multi"],
                         f"没有先试分片：{order[:4]}")

    def test_multipart_after_single_by_default(self):
        """默认 0：仍然是单连接优先，分片只是后手。"""
        total = 20 * 1024 * 1024
        data = payload("sfirst", total)
        order = []
        orig_multi = dl._download_multi_part
        orig_single = dl._try_single_url

        def spy_multi(*a, **k):
            order.append("multi")
            return orig_multi(*a, **k)

        def spy_single(*a, **k):
            order.append("single")
            return orig_single(*a, **k)

        dl._download_multi_part = spy_multi
        dl._try_single_url = spy_single
        try:
            set_opts(large_file_multi_first_bytes=0, disk_aware_slots=0,
                     part_threads=4)
            res = dl.download_files(
                [dl.DownloadTask("sfirst.bin",
                                 [self.srv.url("data", "sfirst", total)],
                                 sha256=sha256_of(data), file_size=total)],
                target_dir=self.tmp)
        finally:
            dl._download_multi_part = orig_multi
            dl._try_single_url = orig_single
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("sfirst.bin", data)
        self.assertEqual(order, ["single"], f"应只走单连接：{order}")


# ======================================================================
# 9. 耐心重试与主池并行（没有串行尾巴）
# ======================================================================
class TestPatientOverlap(TestBase):
    def test_patient_retry_overlaps_main_pool(self):
        """
        一个文件在主池里立刻失败后，耐心重试应当**马上**开始，
        而不是等主池里所有文件都跑完（旧实现是两段串行，会形成尾巴）。
        """
        events = []
        lock = threading.Lock()
        orig = dl._attempt_file

        def fake_attempt(task, target_dir, byte_cb, should_abort, opts,
                         single_mode=False, log=None):
            idx = int(task.rel_path.split("-")[1].split(".")[0])
            ts = time.perf_counter()
            if single_mode:
                with lock:
                    events.append(("patient_start", idx, ts))
                time.sleep(0.05)
                with lock:
                    events.append(("patient_end", idx, time.perf_counter()))
                return dl._AttemptOutcome(True)
            if idx == 0:
                # 第 0 个文件在主池里"秒失败"，立刻降级
                time.sleep(0.02)
                with lock:
                    events.append(("aggr_end", idx, time.perf_counter()))
                return dl._AttemptOutcome(False, "模拟失败")
            time.sleep(0.40)
            with lock:
                events.append(("aggr_end", idx, time.perf_counter()))
            return dl._AttemptOutcome(True)

        dl._attempt_file = fake_attempt
        try:
            set_opts(multi_slots=6, single_slots=2, disk_aware_slots=0)
            tasks = [dl.DownloadTask(f"f-{i}.bin", ["http://127.0.0.1/x"],
                                     sha1="", file_size=1)
                     for i in range(12)]
            t0 = time.perf_counter()
            results = dl.download_files(tasks, target_dir=self.tmp,
                                        threads=None)
            total = time.perf_counter() - t0
        finally:
            dl._attempt_file = orig

        self.assertEqual(len(results), 12)
        patient_starts = [ts for kind, _i, ts in events
                          if kind == "patient_start"]
        aggr_ends = [ts for kind, _i, ts in events if kind == "aggr_end"]
        self.assertEqual(len(patient_starts), 1, "降级文件没有被耐心重试")
        last_main_done = max(aggr_ends)
        self.assertLess(
            min(patient_starts), last_main_done,
            "耐心重试是在主池全部结束之后才开始的（串行尾巴又回来了）")
        # 串行的话总耗时会 ≥ 主池全部耗时 + 耐心耗时
        self.assertLess(total, 1.6,
                        f"耗时 {total:.2f}s，看起来退回了串行两段")

    def test_all_failed_still_reports_every_task(self):
        orig = dl._attempt_file

        def always_fail(task, *a, **k):
            return dl._AttemptOutcome(False, "永久失败")

        set_opts(multi_slots=4, single_slots=2, disk_aware_slots=0,
                 retry_backoff_ms=5, backoff_max_ms=10)
        dl._attempt_file = always_fail
        try:
            tasks = [dl.DownloadTask(f"bad-{i}.bin", ["http://127.0.0.1/x"],
                                     sha1="", file_size=1)
                     for i in range(7)]
            results = dl.download_files(tasks, target_dir=self.tmp,
                                        threads=None)
        finally:
            dl._attempt_file = orig
        self.assertEqual(len(results), 7)
        self.assertTrue(all(not r.ok for r in results))


if __name__ == "__main__":
    unittest.main(verbosity=2)

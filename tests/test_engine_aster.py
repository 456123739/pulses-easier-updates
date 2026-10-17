"""
test_engine_aster.py — Aster 引擎（app/core/engines/aster.py）
================================================================
只用本地 fixture 源，不碰真实网络。覆盖移植过来的几条关键策略：

  1. 多线程自适应分片：大文件必须真的发 Range 请求，且内容/哈希正确
  2. 小文件（< 1 MB）不分片：只有一次请求、没有 Range
  3. 未知大小（chunked）：退化成单线程整文件
  4. 换源：404 / 不支持 Range / 502 都要能自动切到下一个源
  5. 全源失败：ok=False，且不留半成品文件
  6. 哈希不符：整文件单线程重试一次，仍不符才算失败
  7. 中止：should_abort 立刻生效，结果标 aborted
  8. 回调：progress / byte_progress / on_file_* 与 RideX 同形
  9. 多文件共享线程预算，互不串台

运行：  python3 tests/test_engine_aster.py
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
from app.core.engines import aster


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FixtureServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        STATS.reset()
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-aster-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def task(self, rel, urls, data=None, size=0, sha1=None):
        if data is not None:
            size = len(data)
        return dl.DownloadTask(
            rel_path=rel, urls=list(urls),
            sha1=sha1 if sha1 is not None else
            (hashlib.sha1(data).hexdigest() if data is not None else ""),
            file_size=size)

    def run_engine(self, tasks, **kw):
        return aster.download_files(tasks, target_dir=self.tmp, **kw)

    def assert_content(self, rel, expected: bytes):
        p = self.tmp / rel
        self.assertTrue(p.is_file(), f"{rel} 不存在")
        self.assertEqual(p.stat().st_size, len(expected))
        self.assertEqual(p.read_bytes(), expected)


# ======================================================================
# 分片策略
# ======================================================================
class TestSplitting(Base):
    def test_split_policy_is_adaptive(self):
        """PCL 的切分规则：从最大块的尾部切 40%，且只在剩余 > 256KB 时切。"""
        job = aster._Job(dl.DownloadTask(rel_path="x", urls=["http://a/x"]),
                         Path("/tmp/x"))
        job.size = 8 * 1024 * 1024
        src = job.sources[0]
        c0 = aster._Chunk(0, job.size - 1, src)
        c0.running = True          # 已经在跑 → 走"切尾巴"分支
        job.chunks = [c0]
        chunk, got_src = aster._next_spec(job, 8)
        self.assertIsNotNone(chunk)
        self.assertIs(got_src, src)
        self.assertEqual(job.chunks[0].end, chunk.start - 1,
                         "老块必须在切点前收尾")
        undone = 8 * 1024 * 1024
        self.assertEqual(chunk.start, 8 * 1024 * 1024 - 1 - int(undone * 0.4),
                         "切点 = 尾部 40%")
        self.assertEqual(chunk.end, 8 * 1024 * 1024 - 1)

        # 剩余不足 256KB 就不再切
        small = aster._Job(dl.DownloadTask(rel_path="y", urls=["http://a/y"]),
                           Path("/tmp/y"))
        small.size = 300 * 1024
        cs = aster._Chunk(0, small.size - 1, small.sources[0])
        cs.running = True
        small.chunks = [cs]
        self.assertIsNone(aster._next_spec(small, 8))

    def test_big_file_uses_ranges_and_matches(self):
        data = payload("big", 4 * 1024 * 1024)
        url = self.srv.url("data", "big", 4 * 1024 * 1024)
        old_tick = aster.TICK
        aster.TICK = 0.0          # 本地源太快，别让调度间隔吃掉切分时机
        try:
            res = self.run_engine(
                [self.task("mods/big.jar", [url], data=data)], threads=8)
        finally:
            aster.TICK = old_tick
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/big.jar", data)
        self.assertTrue(STATS.ranges, "大文件应该发 Range 请求（多线程分片）")

    def test_small_file_single_request(self):
        data = payload("small", 64 * 1024)
        url = self.srv.url("data", "small", 64 * 1024)
        res = self.run_engine([self.task("mods/small.jar", [url], data=data)],
                              threads=8)
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/small.jar", data)
        self.assertEqual(STATS.ranges, [], "小于 1MB 不该分片")
        self.assertEqual(STATS.requests.get("/data/small"), 1)

    def test_unknown_size_single_thread(self):
        data = payload("chunky", 2 * 1024 * 1024)
        url = self.srv.url("chunked", "chunky", 2 * 1024 * 1024)
        res = self.run_engine([self.task("mods/c.jar", [url])], threads=8)
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/c.jar", data)

    def test_multi_file_share_budget(self):
        tasks, expect = [], {}
        for i in range(4):
            name = f"m{i}"
            data = payload(name, 700 * 1024)
            expect[f"mods/{name}.jar"] = data
            tasks.append(self.task(f"mods/{name}.jar",
                                   [self.srv.url("data", name,
                                                 700 * 1024)],
                                   data=data))
        res = self.run_engine(tasks, threads=4)
        self.assertEqual(len(res), 4)
        self.assertTrue(all(r.ok for r in res), [r.error for r in res])
        for rel, data in expect.items():
            self.assert_content(rel, data)


# ======================================================================
# 换源 / 失败
# ======================================================================
class TestFailover(Base):
    def test_404_falls_back_to_second_source(self):
        data = payload("fb", 512 * 1024)
        urls = [self.srv.url("missing", "fb"), self.srv.url("data", "fb",
                                                            512 * 1024)]
        res = self.run_engine([self.task("mods/fb.jar", urls, data=data)],
                              threads=4)
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/fb.jar", data)

    def test_norange_source_is_dropped(self):
        data = payload("nr", 3 * 1024 * 1024)
        urls = [self.srv.url("norange", "nr", 3 * 1024 * 1024),
                self.srv.url("data", "nr", 3 * 1024 * 1024)]
        res = self.run_engine([self.task("mods/nr.jar", urls, data=data)],
                              threads=6)
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/nr.jar", data)

    def test_502_falls_back(self):
        data = payload("boom", 300 * 1024)
        urls = [self.srv.url("boom", "boom"), self.srv.url("data", "boom",
                                                           300 * 1024)]
        res = self.run_engine([self.task("mods/boom.jar", urls, data=data)],
                              threads=4)
        self.assertTrue(res[0].ok, res[0].error)
        self.assert_content("mods/boom.jar", data)

    def test_all_sources_fail_leaves_nothing(self):
        res = self.run_engine(
            [self.task("mods/gone.jar",
                       [self.srv.url("missing", "gone")])], threads=4)
        self.assertFalse(res[0].ok)
        self.assertTrue(res[0].error)
        self.assertFalse((self.tmp / "mods/gone.jar").exists(),
                         "失败不能留半成品")

    def test_hash_mismatch_fails_after_retry(self):
        data = payload("bad", 256 * 1024)
        t = self.task("mods/bad.jar", [self.srv.url("data", "bad",
                                                    256 * 1024)],
                      data=data, sha1="0" * 40)
        res = self.run_engine([t], threads=4)
        self.assertFalse(res[0].ok)
        self.assertFalse((self.tmp / "mods/bad.jar").exists())
        # 整文件重试会再请求一遍
        self.assertGreaterEqual(STATS.requests.get("/data/bad", 0), 2)

    def test_truncated_source_does_not_hang(self):
        data = payload("trunc", 2 * 1024 * 1024)
        urls = [self.srv.url("truncate", "trunc", 2 * 1024 * 1024)]
        res = self.run_engine([self.task("mods/t.jar", urls, data=data)],
                              threads=4)
        # 截断源补不齐：允许失败，但必须收敛、不能挂死
        self.assertFalse(res[0].ok)


# ======================================================================
# 回调 / 中止
# ======================================================================
class TestCallbacks(Base):
    def test_progress_and_file_callbacks(self):
        data = payload("cb", 1024 * 1024)
        started, done, failed, bytes_seen, prog = [], [], [], [], []
        res = self.run_engine(
            [self.task("mods/cb.jar", [self.srv.url("data", "cb",
                                                    1024 * 1024)],
                       data=data)],
            threads=4,
            progress=lambda d, t, s: prog.append((d, t)),
            byte_progress=lambda rel, got, total: bytes_seen.append(
                (rel, got, total)),
            on_file_started=lambda t: started.append(t.rel_path),
            on_file_done=lambda t: done.append(t.rel_path),
            on_file_failed=lambda t, e: failed.append(t.rel_path))
        self.assertTrue(res[0].ok, res[0].error)
        self.assertEqual(started, ["mods/cb.jar"])
        self.assertEqual(done, ["mods/cb.jar"])
        self.assertEqual(failed, [])
        self.assertTrue(prog)
        self.assertEqual(prog[-1], (1, 1))
        self.assertTrue(bytes_seen)
        rel, got, total = bytes_seen[-1]
        self.assertEqual(rel, "mods/cb.jar")
        self.assertGreater(got, 0)
        self.assertEqual(total, len(data))

    def test_abort_is_fast_and_flagged(self):
        data = payload("ab", 8 * 1024 * 1024)
        url = self.srv.url("slow", "ab", 8 * 1024 * 1024)
        flag = {"stop": False}

        def _abort():
            return flag["stop"]

        def _stop_soon():
            time.sleep(0.4)
            flag["stop"] = True

        threading.Thread(target=_stop_soon, daemon=True).start()
        t0 = time.monotonic()
        res = self.run_engine([self.task("mods/ab.jar", [url])],
                              threads=4, should_abort=_abort)
        self.assertLess(time.monotonic() - t0, 10, "中止不该拖太久")
        self.assertFalse(res[0].ok)
        self.assertTrue(res[0].aborted)


# ======================================================================
# 注册表 / 设置接线
# ======================================================================
class TestRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path as _P
        import stubs
        stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-eng-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        from app.core import database as db
        cls.db = db
        cls.dbdir = _P(cls._tmp.name) / "db"
        db.create_database(cls.dbdir)
        db.set_db_path(cls.dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def test_normalize_and_load(self):
        from app.core import engines
        self.assertEqual(engines.normalize("aster"), "aster")
        self.assertEqual(engines.normalize("ASTER"), "aster")
        self.assertEqual(engines.normalize("pcl"), engines.DEFAULT_ENGINE,
                         "旧名字不该再被当成引擎")
        self.assertEqual(engines.normalize(None), engines.DEFAULT_ENGINE)
        self.assertIs(engines.load("aster"), aster)
        self.assertTrue(callable(engines.load("ridex").download_files))
        self.assertIn("Aster", engines.banner("aster"))
        self.assertIn("RideX", engines.banner("ridex"))
        self.assertIn("星驰", engines.label("aster"))

    def test_engine_setting_roundtrip(self):
        from app.core import engines
        db = self.db
        db.set_download_options({"engine": "aster"})
        self.assertEqual(engines.normalize(
            db.get_download_options().get("engine")), "aster")
        db.set_download_options({"engine": "ridex"})
        self.assertEqual(engines.normalize(
            db.get_download_options().get("engine")), "ridex")

    def test_preferences_exposes_engine_row(self):
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent.parent / "app" / "ui"
               / "widgets" / "preferences_dialog.py").read_text("utf-8")
        self.assertIn("_build_engine_row", src)
        self.assertIn("SegmentedSwitch", src)
        self.assertIn('values["engine"]', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""整合包占用锁 + 变更守卫。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.core import pack_lock as pl

ROOT = Path(__file__).resolve().parents[1]


class LockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-lock-"))
        self.pack = self.tmp / "Pack"
        (self.pack / "mods").mkdir(parents=True)
        (self.pack / "mods" / "a.jar").write_bytes(b"A" * 10)
        self._env = os.environ.get("LOCALAPPDATA")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_release(self):
        lock = pl.PackLock(self.pack, "扫描").acquire()
        try:
            self.assertTrue(lock.path.is_file())
            data = lock.path.read_text(encoding="utf-8")
            self.assertIn("扫描", data)
        finally:
            lock.release()
        # 释放后可以再次拿到
        pl.PackLock(self.pack, "更新").acquire().release()

    def test_second_holder_is_rejected(self):
        first = pl.PackLock(self.pack, "更新").acquire()
        try:
            with self.assertRaises(pl.PackBusy) as ctx:
                pl.PackLock(self.pack, "扫描").acquire()
            self.assertIn("正在", str(ctx.exception))
        finally:
            first.release()

    def test_crash_releases_lock(self):
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.core.pack_lock import PackLock\n"
            "l = PackLock(%r, '更新'); l.acquire()\n"
            "print('locked', flush=True)\n"
            "import os; os._exit(0)\n"
        ) % (str(ROOT), str(self.pack))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        self.assertIn("locked", proc.stdout)
        # 子进程"崩溃"退出后，锁必须由内核自动释放
        pl.PackLock(self.pack, "扫描").acquire().release()

    def test_sigkill_releases_lock_and_pack_stays_writable(self):
        code = (
            "import sys, time; sys.path.insert(0, %r)\n"
            "from app.core.pack_lock import PackLock\n"
            "l = PackLock(%r, '更新'); l.acquire()\n"
            "print('locked', flush=True)\n"
            "time.sleep(30)\n"
        ) % (str(ROOT), str(self.pack))
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            # 1) 锁只锁"锁文件"，整合包里的文件必须照常可改可删
            (self.pack / "mods" / "a.jar").write_bytes(b"EDITED")
            (self.pack / "mods" / "a.jar").unlink()
            (self.pack / "mods" / "b.jar").write_bytes(b"NEW")
            self.assertTrue((self.pack / "mods" / "b.jar").exists())
            # 2) 锁文件不在整合包里
            self.assertNotIn(str(self.pack), str(pl.lock_path_for(self.pack)))
        finally:
            proc.kill()                     # 相当于任务管理器结束进程
            proc.wait(timeout=30)
        # 3) 硬杀之后立刻能重新拿到锁，不需要手工清文件
        pl.PackLock(self.pack, "扫描").acquire().release()

    def test_different_packs_do_not_block(self):
        other = self.tmp / "Other"
        (other / "mods").mkdir(parents=True)
        a = pl.PackLock(self.pack, "更新").acquire()
        try:
            pl.PackLock(other, "更新").acquire().release()
        finally:
            a.release()


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-guard-"))
        self.pack = self.tmp / "Pack"
        (self.pack / "mods").mkdir(parents=True)
        (self.pack / "config").mkdir()
        (self.pack / "logs").mkdir()
        (self.pack / "mods" / "a.jar").write_bytes(b"A" * 10)
        (self.pack / "config" / "x.toml").write_text("k=1\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_change(self):
        snap = pl.snapshot(self.pack)
        self.assertEqual(pl.guard(snap, self.pack), [])

    def test_detects_modify_add_delete(self):
        snap = pl.snapshot(self.pack)
        (self.pack / "mods" / "a.jar").write_bytes(b"B" * 10)
        (self.pack / "mods" / "new.jar").write_bytes(b"N")
        (self.pack / "config" / "x.toml").unlink()
        changed = pl.guard(snap, self.pack)
        self.assertIn("mods/a.jar", changed)
        self.assertIn("mods/new.jar", changed)
        self.assertIn("config/x.toml", changed)

    def test_ignores_logs_and_temp(self):
        snap = pl.snapshot(self.pack)
        (self.pack / "logs" / "latest.log").write_text("x", encoding="utf-8")
        (self.pack / "mods" / ".pulses_tmp_a.jar").write_bytes(b"T")
        self.assertEqual(pl.guard(snap, self.pack), [])

    def test_describe(self):
        self.assertIn("4 个文件", pl.describe(["a", "b", "c", "d"]))


if __name__ == "__main__":
    unittest.main()

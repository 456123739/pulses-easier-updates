"""更新看门狗：只盯计划目标、区分自己改的和别人改的。"""

import tempfile
import time
import unittest
from pathlib import Path

from app.core import pack_watch as W


class WatchCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pe-watch-"))
        (self.root / "mods").mkdir()
        (self.root / "config").mkdir()
        (self.root / "mods" / "a.jar").write_bytes(b"a")
        (self.root / "mods" / "b.jar").write_bytes(b"b")
        (self.root / "config" / "x.toml").write_text("k=1", encoding="utf-8")

    def write(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_quiet_when_nothing_changes(self):
        w = W.PackWatch(self.root, files=["mods/a.jar"])
        self.assertTrue(w)
        self.assertEqual(w.poll(), [])

    def test_detects_modified_target(self):
        w = W.PackWatch(self.root, files=["mods/a.jar"])
        self.write("mods/a.jar", b"changed!")
        self.assertEqual(w.poll(), ["mods/a.jar"])

    def test_ignores_files_outside_plan(self):
        w = W.PackWatch(self.root, files=["mods/a.jar"])
        self.write("mods/b.jar", b"player edit")
        self.assertEqual(w.poll(), [])

    def test_mark_done_hides_our_own_write(self):
        w = W.PackWatch(self.root, files=["mods/a.jar", "mods/b.jar"])
        self.write("mods/a.jar", b"easier wrote this")
        w.mark_done("mods/a.jar")
        # 自己改过的不再算外部改动
        self.assertEqual(w.poll(), [])
        self.assertEqual(w.pending(), ["mods/b.jar"])

    def test_dir_unit_detects_edit_and_new_file(self):
        w = W.PackWatch(self.root, dirs=[("config", ["config/x.toml"])])
        self.assertEqual(w.poll(), [])
        self.write("config/x.toml", b"k=2")
        self.assertEqual(w.poll(), ["config/x.toml"])
        w.rebase(["config/x.toml"])
        self.assertEqual(w.poll(), [])
        self.write("config/new.toml", b"added")
        self.assertEqual(w.poll(), ["config/new.toml"])

    def test_mark_done_clears_dir(self):
        w = W.PackWatch(self.root, dirs=[("config", ["config/x.toml"])])
        w.write_marker = True
        w.mark_done("config/")
        self.assertFalse(w)
        self.write("config/x.toml", b"k=9")
        self.assertEqual(w.poll(), [])

    def test_deleted_target_detected(self):
        w = W.PackWatch(self.root, files=["mods/a.jar"])
        (self.root / "mods" / "a.jar").unlink()
        self.assertEqual(w.poll(), ["mods/a.jar"])

    def test_describe(self):
        text = W.describe(["mods/a.jar", "mods/b.jar", "mods/c.jar",
                           "mods/d.jar"])
        self.assertIn("a.jar", text)
        self.assertIn("4 个文件", text)
        self.assertEqual(W.describe([]), "")


if __name__ == "__main__":
    unittest.main()

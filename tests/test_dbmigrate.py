"""
test_dbmigrate.py — 「数据库位置切换 / 迁移」回归测试
------------------------------------------------
玩家反馈的原始问题：数据库位置只在首次向导里选过，之后没法改。
这里覆盖核心逻辑（迁移的正确性、安全性）与界面接线。

安全底线（每个用例都在守）：
  · 迁移**绝不删除**原目录
  · 校验不过 / 中途失败 → **不切换**
  · cache/ 默认不搬（可重建，往往很大）
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                     # noqa: E402

from app.core import database as db              # noqa: E402
from app.core import dbmigrate as mig            # noqa: E402


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_db(root: Path, tag: str = "a", projects: int = 2) -> Path:
    db.create_database(root)
    _write(root / "projects" / f"{tag}-pack" / "pack.json", f"{{{tag}}}")
    for i in range(projects):
        _write(root / "projects" / f"{tag}-{i}" / "meta.json",
               f'{{"n": {i}, "tag": "{tag}"}}')
    _write(root / "cache" / "update_packs" / "big.bin", "x" * 4096)
    _write(root / "extra" / "note.txt", "hello")
    return root


class TestDescribe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-dbm-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_describe_counts_and_validity(self):
        root = _make_db(self.tmp / "db")
        info = mig.describe(root)
        self.assertTrue(info["valid"])
        self.assertGreaterEqual(info["projects"], 2)
        self.assertGreater(info["bytes"], 0)
        self.assertGreater(info["cache_bytes"], 0)      # cache 单独统计

    def test_describe_missing_dir(self):
        info = mig.describe(self.tmp / "nope")
        self.assertFalse(info["exists"])
        self.assertEqual(info["files"], 0)

    def test_plan_skips_cache_and_reports_conflicts(self):
        src = _make_db(self.tmp / "src", tag="s")
        dst = _make_db(self.tmp / "dst", tag="d")
        p = mig.plan(src, dst)
        self.assertGreater(p["files"], 0)
        self.assertTrue(p["target_valid"])
        self.assertFalse(p["target_is_same"])
        # cache 不参与迁移
        self.assertFalse(any("cache" in c for c in p["conflicts"]))
        self.assertIn("cache", p["skipped_dirs"])


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-dbm2-")
        self.tmp = Path(self._tmp.name)
        self.src = _make_db(self.tmp / "src", tag="s")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migrate_copies_everything_and_keeps_source(self):
        dst = self.tmp / "new" / "pulses_easier_db"
        res = mig.migrate(self.src, dst)
        self.assertTrue(res["ok"], res["msg"])
        self.assertGreater(res["copied"], 0)
        self.assertFalse(mig.verify(self.src, dst))
        self.assertTrue(db.is_valid_database(dst))
        # 内容一致
        self.assertEqual((dst / "extra" / "note.txt").read_text("utf-8"),
                         "hello")
        self.assertTrue(list((dst / "projects").rglob("meta.json")))
        # 原目录一个字都没少
        self.assertTrue(db.is_valid_database(self.src))
        self.assertTrue((self.src / "extra" / "note.txt").is_file())
        # 标记文件不残留
        self.assertFalse(mig.has_incomplete_marker(dst))
        # cache 没搬
        self.assertFalse((dst / "cache" / "update_packs"
                          / "big.bin").exists())

    def test_migrate_into_existing_db_overwrites_but_keeps_extras(self):
        dst = _make_db(self.tmp / "dst", tag="d")
        _write(dst / "projects" / "d-only" / "keep.json", "keep me")
        res = mig.migrate(self.src, dst)
        self.assertTrue(res["ok"], res["msg"])
        self.assertTrue((dst / "projects" / "d-only" / "keep.json").is_file())
        self.assertTrue((dst / "extra" / "note.txt").is_file())

    def test_migrate_progress_is_reported(self):
        seen = []
        res = mig.migrate(self.src, self.tmp / "out",
                          progress=lambda d, t, b, tb: seen.append((d, t)))
        self.assertTrue(res["ok"], res["msg"])
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])       # 走到底

    def test_migrate_creates_empty_dirs(self):
        """新建的空库 projects/ 是空目录 —— 迁移后必须仍然算有效库。"""
        empty = self.tmp / "empty-db"
        db.create_database(empty)
        self.assertEqual(list((empty / "projects").iterdir()), [])
        dst = self.tmp / "empty-out"
        res = mig.migrate(empty, dst)
        self.assertTrue(res["ok"], res["msg"])
        self.assertTrue((dst / "projects").is_dir(),
                        "空目录没有被创建 → 目标库会被判为无效")
        self.assertTrue(db.is_valid_database(dst))
        ok, msg = mig.switch_to(dst)
        self.assertTrue(ok, msg)

    def test_migrate_refuses_same_path(self):
        res = mig.migrate(self.src, self.src)
        self.assertFalse(res["ok"])
        self.assertIn("同一个目录", res["msg"])

    def test_migrate_refuses_invalid_source(self):
        res = mig.migrate(self.tmp / "empty", self.tmp / "out2")
        self.assertFalse(res["ok"])
        self.assertGreater(len(res["msg"]), 0)

    def test_copy_failure_leaves_marker_and_no_switch(self):
        dst = self.tmp / "out3"
        with mock.patch.object(mig.shutil, "copy2",
                               side_effect=OSError(13, "denied")):
            res = mig.migrate(self.src, dst)
        self.assertFalse(res["ok"])
        self.assertTrue(res["failed"])
        self.assertTrue(mig.has_incomplete_marker(dst),
                        "失败必须留下标记，免得被当成完整库")
        self.assertFalse(res["ok"])

    def test_verify_detects_size_mismatch(self):
        dst = self.tmp / "out4"
        mig.migrate(self.src, dst)
        target = dst / "extra" / "note.txt"
        target.write_text("tampered", encoding="utf-8")
        bad = mig.verify(self.src, dst)
        self.assertEqual([b["rel"] for b in bad], ["extra/note.txt"])

    def test_cancel_keeps_source_intact(self):
        res = mig.migrate(self.src, self.tmp / "out5",
                          should_abort=lambda: True)
        self.assertFalse(res["ok"])
        self.assertIn("取消", res["msg"])
        self.assertEqual(res["copied"], 0)
        self.assertTrue(db.is_valid_database(self.src))


class TestSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-dbm3-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        self.tmp = Path(self._tmp.name)
        self.a = _make_db(self.tmp / f"a{id(self)}", tag="a")
        self.b = _make_db(self.tmp / f"b{id(self)}", tag="b")

    def test_switch_to_valid_db(self):
        ok, msg = mig.switch_to(self.a)
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_db_path(), self.a)
        ok2, _ = mig.switch_to(self.b)
        self.assertTrue(ok2)
        self.assertEqual(db.get_db_path(), self.b)

    def test_switch_rejects_invalid_dir(self):
        bad = self.tmp / "not-a-db"
        bad.mkdir(parents=True, exist_ok=True)
        ok, msg = mig.switch_to(bad)
        self.assertFalse(ok)
        self.assertIn("不是有效", msg)

    def test_switch_rejects_dir_with_incomplete_marker(self):
        _write(self.b / mig.MARKER, "started")
        ok, msg = mig.switch_to(self.b)
        self.assertFalse(ok)
        self.assertIn("没迁移完", msg)

    def test_create_at_makes_valid_empty_db(self):
        dst = self.tmp / "fresh" / "pulses_easier_db"
        ok, msg = mig.create_at(dst)
        self.assertTrue(ok, msg)
        self.assertTrue(db.is_valid_database(dst))
        self.assertEqual(list((dst / "projects").iterdir()), [])


class TestUiWiring(unittest.TestCase):
    """「更多 → 数据库位置」确实接上了（桩环境里构造真窗口）。"""

    @classmethod
    def setUpClass(cls):
        if "customtkinter" not in sys.modules:
            stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-dbm4-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def test_more_panel_has_db_button(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        Window = type("WindowClass", (MainWindow, AppBase), {})
        app = Window()
        panel = app.sidebar.more_panel if hasattr(app.sidebar, "more_panel") \
            else None
        self.assertIsNotNone(panel, "找不到「更多」面板")
        self.assertTrue(hasattr(panel, "db_btn"), "「更多」里没有数据库按钮")
        self.assertTrue(callable(getattr(panel, "_on_db_click", None)))

    def test_db_panel_module_imports(self):
        from app.ui.widgets.db_panel import DatabaseDialog, open_db_settings
        self.assertTrue(callable(open_db_settings))
        self.assertTrue(isinstance(DatabaseDialog, type))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
test_polish.py — 第三批（健壮性与整洁）回归测试
------------------------------------------------
  P2-1  三处"是否会应用"的判定收敛为 apply_rules，且结果一致
  P2-2  根目录散文件按内容比对（不再恒判 MODIFIED）；index 根文件哈希生效
  P2-3  死配置项真正生效：log_max_lines / progress_throttle_ms /
        hash_threads；defer_to_single_after 已删除
  P2-4  单连接续传校验 Content-Range 起点
  P2-5  嵌套 overrides（index 在一层目录里）也能找到内容根
  P2-6  死模块已删除且没有任何引用
  P2-7  开发者端：版本取自源包、导出临时目录清理、勾选即写库、
        导入配置恢复白名单/导出选项、接受 .eapack
  P2-8  预览窗口唯一文件名 + 子进程失败自动兜底
  P2-9  main.py 退出前清理工作目录

运行：  python3 tests/test_polish.py
"""

import inspect
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                     # noqa: E402


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-pol-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        from app.core import database as db
        cls.db = db
        cls.dbdir = Path(cls._tmp.name) / "db"
        db.create_database(cls.dbdir)
        db.set_db_path(cls.dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=self._tmp.name))


# ======================================================================
# P2-1 判定收敛
# ======================================================================
class TestApplyRulesAgree(_Base):
    """三处判定必须完全一致：apply_rules / build_plan / change_list。"""

    def _cases(self):
        from app.config import ChangeKind
        from app.core.differ import Change
        combos = []
        for kind in (ChangeKind.ADDED, ChangeKind.MODIFIED, ChangeKind.DELETED):
            for folder_level in (False, True):
                for checked in (True, False):
                    for strat in ("完全匹配", "替换重名", "跳过重名"):
                        combos.append(Change(
                            Path("config/x.toml"), kind,
                            is_folder_level=folder_level))
                        combos[-1]._ctx = (checked, strat)
        return combos

    def test_three_implementations_agree(self):
        from app.core import apply_rules
        from app.core.updater import build_plan
        from app.ui.widgets.change_list import _will_skip

        for ch in self._cases():
            checked, strat = ch._ctx
            checked_map = {"config": checked}
            strategies = {"config": strat}

            rules = apply_rules.will_skip(ch, checked_map, strategies)
            ui = _will_skip(ch, checked_map, strategies)

            class _Diff:
                added = [ch] if ch.kind.value == "added" else []
                modified = [ch] if ch.kind.value == "modified" else []
                deleted = [ch] if ch.kind.value == "deleted" else []
                total = 1

            plan = build_plan(_Diff(), checked_map, strategies)
            planned_skip = bool(plan.skip)

            label = (f"{ch.kind.value}/folder={ch.is_folder_level}/"
                     f"checked={checked}/{strat}")
            self.assertEqual(rules, ui, f"apply_rules 与界面灰显不一致：{label}")
            self.assertEqual(rules, planned_skip,
                             f"apply_rules 与实际计划不一致：{label}")

    def test_unchecked_means_skip_everywhere(self):
        from app.core.differ import Change
        from app.config import ChangeKind
        from app.core.updater import build_plan
        from app.ui.widgets.change_list import _will_skip
        ch = Change(Path("mods/a.jar"), ChangeKind.ADDED)
        checked = {"mods": False}
        self.assertTrue(_will_skip(ch, checked, {}))
        plan = build_plan(types.SimpleNamespace(
            added=[ch], modified=[], deleted=[], total=1), checked, {})
        self.assertEqual(plan.skip, [Path("mods/a.jar")])
        self.assertEqual(plan.copy, [])


# ======================================================================
# P2-2 根文件内容比对
# ======================================================================
class TestRootFileDiff(_Base):
    def test_identical_root_file_is_not_modified(self):
        from app.core.differ import diff_packs_parallel
        old = self.tmp / "o1"
        new = self.tmp / "n1"
        _write(old / "options.txt", "fov:90")
        _write(new / "options.txt", "fov:90")
        d = diff_packs_parallel(old, new, whitelist=["mods"],
                                index_hashes={})
        self.assertEqual(d.modified, [])

    def test_changed_root_file_is_modified(self):
        from app.core.differ import diff_packs_parallel
        old = self.tmp / "o2"
        new = self.tmp / "n2"
        _write(old / "options.txt", "fov:90")
        _write(new / "options.txt", "fov:70")
        d = diff_packs_parallel(old, new, whitelist=["mods"],
                                index_hashes={})
        self.assertEqual([c.rel_path.name for c in d.modified],
                         ["options.txt"])

    def test_index_root_hash_is_used_as_target(self):
        import hashlib
        from app.core.differ import diff_packs_parallel
        old = self.tmp / "o3"
        new = self.tmp / "n3"
        _write(old / "options.txt", "fov:90")
        _write(new / "options.txt", "fov:70")
        target = hashlib.sha1(b"fov:70").hexdigest()
        d = diff_packs_parallel(
            old, new, whitelist=["mods"],
            index_hashes={"options.txt": {"sha1": target}})
        self.assertEqual(len(d.modified), 1)
        # index 声明的目标恰好等于本地内容 → 不需要更新
        local = hashlib.sha1(b"fov:90").hexdigest()
        d2 = diff_packs_parallel(
            old, new, whitelist=["mods"],
            index_hashes={"options.txt": {"sha1": local}})
        self.assertEqual(d2.modified, [])


# ======================================================================
# P2-3 配置项生效
# ======================================================================
class TestConfigReallyUsed(_Base):
    def test_defer_to_single_after_removed(self):
        from app.core import database as db
        from app.core import downloader as dl
        self.assertNotIn("defer_to_single_after", db.DEFAULT_DOWNLOAD_OPTIONS)
        self.assertNotIn("defer_to_single_after", dl._DEFAULTS)
        keys = {k for k, *_rest in db.DOWNLOAD_OPTION_META}
        self.assertNotIn("defer_to_single_after", keys)

    def test_hash_threads_used_by_scan(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn("get_compare_options", src)
        self.assertIn("hash_threads", src)
        self.assertNotIn("threads=16", src)

    def test_progress_throttle_used(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn("progress_throttle_ms", src)
        self.assertIn("_byte_throttle_s", src)

    def test_log_panel_respects_max_lines(self):
        from app.ui.widgets.log_panel import LogPanel
        self.db.set_ui_options({"log_max_lines": 60})
        try:
            panel = LogPanel(None)
            self.assertEqual(panel._max_lines, 60)
            panel.set_max_lines(50)
            self.assertEqual(panel._max_lines, 50)
        finally:
            self.db.set_ui_options({"log_max_lines": 500})


# ======================================================================
# P2-4 单连接续传 Range 校验
# ======================================================================
class TestSingleResumeRangeCheck(_Base):
    def _resp(self, status: int, headers: dict):
        class _R:
            pass
        r = _R()
        r.status = status
        r.getheader = lambda k, d=None: headers.get(k, d)
        return r

    def test_start_match(self):
        from app.core.downloader import _content_range_start_ok
        resp = self._resp(206, {"Content-Range": "bytes 100-999/1000"})
        self.assertTrue(_content_range_start_ok(resp, 100))

    def test_start_mismatch(self):
        from app.core.downloader import _content_range_start_ok
        resp = self._resp(206, {"Content-Range": "bytes 0-999/1000"})
        self.assertFalse(_content_range_start_ok(resp, 100))

    def test_missing_header_rejected_for_206(self):
        from app.core.downloader import _content_range_start_ok
        self.assertFalse(_content_range_start_ok(self._resp(206, {}), 100))
        self.assertTrue(_content_range_start_ok(self._resp(200, {}), 100))


# ======================================================================
# P2-5 嵌套 overrides
# ======================================================================
class TestNestedOverrides(_Base):
    def test_locate_content_root_variants(self):
        from app.core.mrpack import locate_content_root
        root = self.tmp / "ex1"
        _write(root / "overrides" / "a.txt", "1")
        got, fb = locate_content_root(root)
        self.assertEqual(got, root / "overrides")
        self.assertFalse(fb)

        nested = self.tmp / "ex2"
        _write(nested / "MyPack" / "overrides" / "a.txt", "1")
        got2, fb2 = locate_content_root(nested)
        self.assertEqual(got2, nested / "MyPack" / "overrides")
        self.assertFalse(fb2)

        bare = self.tmp / "ex3"
        _write(bare / "mods" / "a.jar", "1")
        got3, fb3 = locate_content_root(bare)
        self.assertEqual(got3, bare)
        self.assertTrue(fb3)

    def test_parse_mrpack_reads_nested_overrides(self):
        from app.core.mrpack import parse_mrpack
        zpath = self.tmp / "nested.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("MyPack/modrinth.index.json",
                        json.dumps({"files": [], "name": "N"}))
            zf.writestr("MyPack/overrides/config/x.toml", "k = 1")
        pack, err = parse_mrpack(zpath)
        self.assertFalse(err)
        self.assertEqual(pack.overrides_files, ["config/x.toml"])


# ======================================================================
# P2-6 死代码
# ======================================================================
class TestDeadCodeRemoved(unittest.TestCase):
    DEAD = ("app/core/pack_builder.py", "app/core/metadata.py",
            "app/ui/widgets/option_panel.py", "app/ui/widgets/md_viewer.py",
            "app/ui/widgets/change_tree.py")

    def test_dead_modules_gone(self):
        for rel in self.DEAD:
            self.assertFalse((_ROOT / rel).exists(), f"{rel} 仍然存在")

    def test_no_references(self):
        bad = []
        for py in list((_ROOT / "app").rglob("*.py")) + [_ROOT / "main.py"]:
            text = py.read_text(encoding="utf-8")
            for name in ("pack_builder", "OptionPanel", "MarkdownModal",
                         "ChangeTree", "META_KEY_", "DEFAULT_CHECKED",
                         "extract_meta_from_zip"):
                if name in text:
                    bad.append((py.relative_to(_ROOT).as_posix(), name))
        self.assertEqual(bad, [], f"仍有对已删代码的引用：{bad}")

    def test_checkpoint_public_api(self):
        from app.core import checkpoint as cp
        for name in ("Checkpoint", "save_checkpoint", "clear_checkpoint",
                     "scan_checkpoints"):
            self.assertTrue(hasattr(cp, name), f"checkpoint 缺少 {name}")


# ======================================================================
# P2-7 开发者端
# ======================================================================
class TestDeveloperViewPolish(_Base):
    def _view(self):
        from app.ui.developer_view import DeveloperView
        return DeveloperView(None)

    def test_version_not_hardcoded(self):
        view = self._view()
        self.assertEqual(view.version, "")
        src = (_ROOT / "app" / "ui" / "developer_view.py").read_text(
            encoding="utf-8")
        self.assertNotIn('self.version = "1.0.0"', src)

    def test_accepts_eapack(self):
        view = self._view()
        # 让 after 立即执行，避免解析线程留下临时目录
        view.after = lambda ms, fn=None, *a, **k: (
            fn(*a, **k) if callable(fn) else None)
        target = self.tmp / "x.eapack"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("modrinth.index.json",
                        json.dumps({"files": [], "name": "P"}))
            zf.writestr("overrides/", b"")
        view._on_pack_dropped([target])
        self.assertEqual(view.source_zip, target)
        view._clear_pack_state()

    def test_export_options_written_on_toggle(self):
        """勾选即写库（桩控件存不住 BooleanVar，这里直接验证接线）。"""
        view = self._view()
        self.db.set_export_options({"min_size": False})
        with mock.patch.object(view.export_options, "get_options",
                               return_value={"min_size": True,
                                             "compress": True}):
            view._on_export_options_changed()
        self.assertTrue(self.db.get_export_options()["min_size"])
        self.db.set_export_options({"min_size": False})

    def test_export_options_panel_has_on_change_wired(self):
        view = self._view()
        self.assertIsNotNone(view.export_options.on_change)
        self.assertEqual(view.export_options.on_change.__func__,
                         type(view)._on_export_options_changed)

    def test_export_tmp_cleaned_after_build(self):
        view = self._view()
        tmp = tempfile.TemporaryDirectory(prefix="pulses_dev_")
        view.export_tmp_dir = tmp
        view.tmp_dir = tmp
        view._cleanup_export_tmp()
        self.assertIsNone(view.export_tmp_dir)
        self.assertFalse(Path(tmp.name).exists())

    def test_import_profile_restores_whitelist_and_options(self):
        from app.ui.widgets.strategy_table import StrategyTable
        table = StrategyTable(None)
        profile = {"strategies": {"mods": "跳过重名"},
                   "blacklist": ["config"],
                   "whitelist": ["mods", "kubejs"],
                   "export_options": {"min_size": True}}
        import tkinter.filedialog as fd
        saved = getattr(fd, "askopenfilename", None)
        fd.askopenfilename = lambda **k: str(self._profile_file(profile))
        try:
            table._on_import()
        finally:
            if saved is None:
                try:
                    del fd.askopenfilename
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                fd.askopenfilename = saved
        self.assertEqual(self.db.get_whitelist(), ["mods", "kubejs"])
        self.assertTrue(self.db.get_export_options()["min_size"])
        self.db.set_whitelist(["mods", "resourcepacks", "shaderpacks"])
        self.db.set_export_options({"min_size": False})

    def _profile_file(self, profile: dict) -> Path:
        p = self.tmp / "profile.json"
        p.write_text(json.dumps(profile), encoding="utf-8")
        return p

    def test_strategy_table_default_matches_config(self):
        from app.config import Strategy, default_strategy_for_dir
        from app.ui.widgets.strategy_table import StrategyTable
        table = StrategyTable(None)
        table.set_folders(["mods", "config", "saves"])
        got = table.get_strategies()
        self.assertEqual(got["mods"], Strategy.FULL_MATCH.value)
        self.assertEqual(got["config"],
                         default_strategy_for_dir("config").value)
        self.assertEqual(got["saves"], Strategy.SKIP_SAME.value)


# ======================================================================
# P2-8 预览窗口
# ======================================================================
class TestPreviewWindow(_Base):
    def test_unique_filename_and_fallback_on_failure(self):
        from app.core import markdown_renderer as mr
        seen = []

        class _Proc:
            returncode = 1

            def wait(self, timeout=None):
                return 1

        with mock.patch.object(mr.subprocess, "Popen",
                               side_effect=lambda *a, **k: seen.append(a[0])
                               or _Proc()):
            opened = []
            with mock.patch("webbrowser.open",
                            side_effect=lambda u: opened.append(u)):
                ok, err = mr.open_preview_window("<html></html>")
                self.assertTrue(ok, err)
                # 等待看护线程（非 0 退出 → 浏览器兜底）
                import time
                for _ in range(50):
                    if opened:
                        break
                    time.sleep(0.02)
        self.assertTrue(seen, "没有启动预览子进程")
        names = [Path(seen[0][-1]).name]
        self.assertTrue(names[0].startswith(mr._PREVIEW_PREFIX))
        self.assertIn("pulses_easier_preview_", names[0])
        self.assertTrue(opened, "子进程失败后没有浏览器兜底")

    def test_two_previews_use_different_files(self):
        from app.core import markdown_renderer as mr
        paths = []

        class _Proc:
            def wait(self, timeout=None):
                return 0

        with mock.patch.object(mr.subprocess, "Popen",
                               side_effect=lambda *a, **k:
                               paths.append(a[0][-1]) or _Proc()):
            mr.open_preview_window("<html>1</html>")
            mr.open_preview_window("<html>2</html>")
        self.assertEqual(len(set(paths)), 2)


# ======================================================================
# P2-9 main.py 退出清理
# ======================================================================
class TestExitCleanup(_Base):
    def test_main_calls_exit_cleanup(self):
        src = (_ROOT / "main.py").read_text(encoding="utf-8")
        i_clean = src.index("app.on_exit_cleanup()")
        i_exit = src.index("os._exit(0)")
        self.assertLess(i_clean, i_exit,
                        "on_exit_cleanup 必须在 os._exit 之前调用")

    def test_mainwindow_has_exit_cleanup(self):
        from app.ui.main_window import MainWindow
        self.assertTrue(hasattr(MainWindow, "on_exit_cleanup"))

    def test_exit_cleanup_removes_workdir(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        WindowClass = type("WindowClass", (MainWindow, AppBase), {})
        app = WindowClass()
        work = tempfile.TemporaryDirectory(prefix="pulses_play_")
        app.player_view._temp_dir = work
        app.on_exit_cleanup()
        self.assertFalse(Path(work.name).exists())
        self.assertIsNone(app.player_view._temp_dir)


if __name__ == "__main__":
    unittest.main(verbosity=2)

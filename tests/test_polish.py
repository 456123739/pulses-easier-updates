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
# 引擎命名（RideX / 锐驰引擎）
# ======================================================================
class TestEngineBranding(_Base):
    def test_constants(self):
        from app.core import downloader as dl
        self.assertEqual(dl.ENGINE_NAME, "RideX")
        self.assertEqual(dl.ENGINE_NAME_CN, "锐驰引擎")
        self.assertIn("RideX", dl.ENGINE_FULL_NAME)
        self.assertIn("锐驰引擎", dl.ENGINE_FULL_NAME)

    def test_banner_and_summary(self):
        from app.core import downloader as dl
        banner = dl.engine_banner()
        self.assertIn("RideX", banner)
        self.assertIn("锐驰引擎", banner)
        summary = dl.disk_policy_summary(self.tmp)
        self.assertTrue(summary.startswith("RideX（锐驰引擎）"), summary)
        plain = dl.disk_policy_summary(self.tmp, with_engine=False)
        self.assertFalse(plain.startswith("RideX"), plain)

    def test_docstring_and_ui_reference(self):
        src = (_ROOT / "app" / "core" / "downloader.py").read_text(
            encoding="utf-8")
        self.assertIn("RideX（锐驰引擎）", src)
        pref = (_ROOT / "app" / "ui" / "widgets"
                / "preferences_dialog.py").read_text(encoding="utf-8")
        self.assertIn("dl_mod.engine_banner()", pref)
        pv = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn("engine_banner", pv)


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


# ======================================================================
# 后续追加：策略锁定 / 开发者端关闭守护 / 续传沿用自定义策略
# ======================================================================
class TestProtectedStrategyLocked(_Base):
    """mods 的策略不允许手动切换，固定"完全匹配"。"""

    def _table(self):
        from app.ui.widgets.strategy_table import StrategyTable
        t = StrategyTable(None)          # 默认 protected_folders=["mods"]
        t.set_folders(["mods", "config"])
        return t

    def test_recommended_cannot_change_mods(self):
        from app.config import Strategy
        t = self._table()
        t.set_strategies({"mods": Strategy.SKIP_SAME.value,
                          "config": Strategy.SKIP_SAME.value})
        got = t.get_strategies()
        self.assertEqual(got["mods"], Strategy.FULL_MATCH.value)
        self.assertEqual(got["config"], Strategy.SKIP_SAME.value)

    def test_manual_switch_ignored(self):
        from app.config import Strategy
        t = self._table()
        rows = t._rows
        rows["mods"].group.set_value(Strategy.REPLACE_SAME, animate=False)
        t._on_strategy("mods")
        self.assertEqual(t.get_strategies()["mods"],
                         Strategy.FULL_MATCH.value)

    def test_group_is_locked_and_label_marked(self):
        t = self._table()
        self.assertTrue(t._rows["mods"].group.is_locked())
        self.assertFalse(t._rows["config"].group.is_locked())
        src = (_ROOT / "app" / "ui" / "widgets" / "strategy_table.py").read_text(
            encoding="utf-8")
        self.assertIn("（固定）", src)

    def test_unify_skips_mods(self):
        from app.config import Strategy
        t = self._table()
        t._on_unify()
        self.assertEqual(t.get_strategies()["mods"],
                         Strategy.FULL_MATCH.value)

    def test_import_profile_cannot_unlock_mods(self):
        from app.config import Strategy
        t = self._table()
        profile = {"strategies": {"mods": "跳过重名"},
                   "blacklist": [], "whitelist": ["mods"],
                   "export_options": {}}
        pf = self.tmp / "p.json"
        pf.write_text(json.dumps(profile), encoding="utf-8")
        import tkinter.filedialog as fd
        saved = getattr(fd, "askopenfilename", None)
        fd.askopenfilename = lambda **k: str(pf)
        try:
            t._on_import()
        finally:
            if saved is None:
                try:
                    del fd.askopenfilename
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                fd.askopenfilename = saved
        self.assertEqual(t.get_strategies()["mods"],
                         Strategy.FULL_MATCH.value)

    def test_player_view_marks_mods_protected(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn('protected_folders=["mods"]', src)


class TestCloseGuardLayers(_Base):
    """开发者端导出期间不允许关闭，优先级高于玩家端的告警。"""

    def _window(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        return type("W", (MainWindow, AppBase), {})()

    def test_developer_guard_wins(self):
        mw = self._window()
        calls = []
        mw.install_close_guard("player",
                               lambda: calls.append("player"))
        mw.install_close_guard("developer",
                               lambda: calls.append("developer"))
        mw._dispatch_close()
        self.assertEqual(calls, ["developer"], "导出期间的守卫必须优先")
        mw.release_close_guard("developer")
        mw._dispatch_close()
        self.assertEqual(calls, ["developer", "player"])
        mw.release_close_guard("player")
        self.assertEqual(mw._close_guards, {})

    def test_dev_view_blocks_close_with_alert(self):
        from app.ui import developer_view as dv
        view = dv.DeveloperView(None)
        seen = []
        with mock.patch.object(dv, "alert",
                               side_effect=lambda *a, **k: seen.append(a)):
            view._on_close_blocked()
        self.assertEqual(len(seen), 1, "没有弹出'无法关闭'提示")

    def test_build_install_and_release(self):
        src = (_ROOT / "app" / "ui" / "developer_view.py").read_text(
            encoding="utf-8")
        i_install = src.index("self._install_close_guard()")
        i_build = src.index("self._set_locked(True)")
        self.assertLess(i_build, i_install,
                        "开始制作后必须立刻装上关闭守卫")
        self.assertIn("self._uninstall_close_guard()", src)

    def test_player_uses_guard_manager(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn('installer("player", self._on_close_request)', src)
        self.assertIn('releaser("player")', src)


class TestResumeReusesCustomStrategies(_Base):
    """续传时沿用玩家上次为这个更新包自定义的策略。"""

    def _view(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        app = type("W", (MainWindow, AppBase), {})()
        return app, app.player_view

    def _pack_zip(self) -> Path:
        z = self.tmp / "pack.eapack"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("modrinth.index.json",
                        json.dumps({"files": [], "name": "P"}))
            zf.writestr("ea_settings.json", "{}")
            zf.writestr("ea_manifest.json", "{}")
            zf.writestr("overrides/", b"")
        return z

    def test_write_resume_records_strategies(self):
        app, pv = self._view()
        pack = self._pack_zip()
        inst = self.tmp / "inst"
        (inst / "mods").mkdir(parents=True)
        pv.pack_info = types.SimpleNamespace(name="P", version="1",
                                             path=inst)
        pv.update_zip = pack
        pv._cache_root = self.tmp / "cache"
        pv._cache_root.mkdir(exist_ok=True)
        pv._download_tasks = [types.SimpleNamespace(rel_path="mods/a.jar")]
        pv._baseline_strategies = {"mods": "完全匹配",
                                   "config": "替换重名"}
        pv.strategy_table.set_folders(["mods", "config"])
        pv.strategy_table.set_strategies({"config": "跳过重名"})
        pv.strategy_table.set_blacklist([])
        pv._write_resume()
        data = json.loads((pv._cache_root / "resume.json")
                          .read_text("utf-8"))
        self.assertEqual(data["strategies"]["config"], "跳过重名")
        self.assertTrue(data["strategies_customized"],
                        "偏离基线却没有标记为已自定义")

    def test_customized_flag_false_for_defaults(self):
        app, pv = self._view()
        pv._baseline_strategies = {"mods": "完全匹配",
                                   "config": "替换重名"}
        pv.strategy_table.set_folders(["mods", "config"])
        pv.strategy_table.set_strategies({"config": "替换重名"})
        pv.strategy_table.set_blacklist([])
        self.assertFalse(pv._is_strategy_customized())

    def test_resume_from_reads_saved_strategies(self):
        app, pv = self._view()
        pack = self._pack_zip()
        inst = self.tmp / "inst2"
        (inst / "mods").mkdir(parents=True)
        data = {
            "zip_path": str(pack), "pack_root": str(inst),
            "completed": [], "failed": [], "total": 0,
            "strategies": {"config": "跳过重名"},
            "checked": {"config": False},
            "strategies_customized": True,
        }
        self.assertTrue(pv.resume_from(data))
        self.assertEqual(pv._resume_strategies, {"config": "跳过重名"})
        self.assertEqual(pv._resume_checked, {"config": False})

    def test_restore_applies_to_table(self):
        app, pv = self._view()
        pv._resume_strategies = {"config": "跳过重名"}
        pv._resume_checked = {"config": False}
        pv.strategy_table.set_folders(["mods", "config"])
        self.assertTrue(pv._restore_resume_strategies())
        self.assertEqual(pv.strategy_table.get_strategies()["config"],
                         "跳过重名")
        self.assertFalse(pv.strategy_table.get_checked()["config"])
        # 恢复一次即作废，避免影响后续普通更新
        self.assertFalse(pv._restore_resume_strategies())

    def test_scan_done_restores_before_collecting_tasks(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        i_restore = src.index("self._restore_resume_strategies()")
        i_collect = src.index("self._download_tasks = "
                              "self._collect_download_tasks_from_diff",
                              i_restore)
        self.assertLess(i_restore, i_collect,
                        "必须先恢复策略再算下载任务")

    def test_resume_written_at_download_start(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        i_marker = src.index("# 一开始就落一份续传记录")
        i_thread = src.index("self._download_thread = threading.Thread",
                             i_marker)
        self.assertLess(i_marker, i_thread,
                        "续传记录必须在下载线程启动前落盘")


# ======================================================================
# 启动回调 / 模块级引用：不许出现"引用了不存在的东西"
# （Pylance 报的 reportAttributeAccessIssue / F821 就是这一类，
#   运行时表现是 AttributeError / NameError，而且往往被 try 吞掉）
# ======================================================================
class TestNoDanglingReferences(unittest.TestCase):
    """不许出现"引用了不存在的东西"（启动回调 / import 期名字错误）。"""

    @classmethod
    def setUpClass(cls):
        if "customtkinter" not in sys.modules:
            stubs.install()

    def test_after_callbacks_exist(self):
        """
        `self.after(ms, self._xxx)` 会在注册的那一刻就取属性——
        方法名写错/被误删时，它**后面**注册的回调全都失效。
        真实事故：_check_database / _log_ignored_count 被误删，导致
        on_boot_done 抛 AttributeError，"上次更新没正常结束"与"继续上次
        更新"两个提示从来没出现过。
        """
        import importlib
        import re
        ui_root = Path(__file__).resolve().parent.parent / "app" / "ui"
        problems: list[str] = []
        scanned = 0
        for path in sorted(ui_root.rglob("*.py")):
            rel = path.relative_to(ui_root.parent.parent).with_suffix("")
            mod_name = ".".join(rel.parts)
            try:
                mod = importlib.import_module(mod_name)
            except Exception:  # noqa: BLE001
                continue
            src = path.read_text(encoding="utf-8")
            # 只认"裸方法引用"（self._x 后面紧跟 , 或 )）：
            # `self.after(0, self.widget.destroy)` 是实例属性链，不在检查范围
            names = set(re.findall(
                r"after\(\s*\d+\s*,\s*self\.([A-Za-z_][A-Za-z0-9_]*)\s*[,)]",
                src))
            if not names:
                continue
            scanned += 1
            # 允许三类来源：本模块里 def 出来的、self.x = ... 赋值的、
            # 以及 tkinter 自带方法（桩环境下拿不到基类，只能列白名单）
            tk_base = {"destroy", "withdraw", "deiconify", "lift", "update",
                       "quit", "focus_force", "winfo_toplevel"}
            defined = set(re.findall(r"def ([A-Za-z_][A-Za-z0-9_]*)", src))
            defined |= set(re.findall(
                r"self\.([A-Za-z_][A-Za-z0-9_]*)\s*=", src))
            defined |= tk_base
            classes = [v for v in vars(mod).values() if isinstance(v, type)]
            for n in sorted(names):
                if n in defined:
                    continue
                if any(hasattr(c, n) for c in classes):
                    continue
                problems.append(f"{mod_name}.{n}")
        self.assertGreater(scanned, 0, "没扫到任何 after 回调，正则失效了")
        self.assertEqual(problems, [],
                         f"after 回调引用了不存在的属性：{problems}")

    def test_on_boot_done_runs_without_attribute_error(self):
        """启动收尾不许抛异常（抛了就会静默丢掉后面的自检/续传提示）。"""
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        Window = type("WindowClass", (MainWindow, AppBase), {})
        app = Window()
        self.assertFalse(app._boot_done)
        app.on_boot_done()
        self.assertTrue(app._boot_done)

    def test_boot_callbacks_are_callable(self):
        from app.ui.main_window import MainWindow
        for name in ("_check_database", "_check_apply_recovery",
                     "_check_resume", "_log_ignored_count"):
            self.assertTrue(callable(getattr(MainWindow, name, None)),
                            f"{name} 不存在或不可调用")

    def test_every_app_module_imports(self):
        """任何一个模块在 import 期就抛 NameError/AttributeError 都要挡住。"""
        import importlib
        root = Path(__file__).resolve().parent.parent / "app"
        failed: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root.parent).with_suffix("")
            mod = ".".join(rel.parts)
            if mod.endswith(".__init__"):
                mod = mod[:-len(".__init__")]
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{mod}: {type(e).__name__}: {e}")
        self.assertEqual(failed, [], "模块导入失败：" + "; ".join(failed))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
test_basic.py — 基础测试：编译、全模块导入、启动链路、配置一致性。

在没有图形环境（无 tkinter / 无 X）的机器上，用桩模块覆盖第三方 GUI 依赖，
只验证「导入期错误 / 语法错误 / 配置漂移」，不做界面渲染。

运行：  python3 tests/test_basic.py
"""

import compileall
import importlib
import inspect
import pkgutil
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                    # noqa: E402

GUI_MODULES = ("customtkinter",)                # 需要桩的第三方包


class TestCompile(unittest.TestCase):
    def test_compileall(self):
        ok = compileall.compile_dir(str(_ROOT / "app"), quiet=2,
                                    force=True)
        self.assertTrue(ok, "app/ 下存在语法错误")
        ok = compileall.compile_file(str(_ROOT / "main.py"), quiet=2,
                                     force=True)
        self.assertTrue(ok, "main.py 存在语法错误")


class TestImportsCore(unittest.TestCase):
    """核心模块用真实依赖导入（不得依赖 GUI 库）。"""

    def test_core_modules_import(self):
        import app
        failed = []
        count = 0
        for m in pkgutil.walk_packages(app.__path__, "app."):
            if ".ui" in m.name:
                continue
            try:
                importlib.import_module(m.name)
                count += 1
            except Exception as e:  # noqa: BLE001
                failed.append((m.name, f"{type(e).__name__}: {e}"))
        self.assertGreater(count, 15)
        self.assertEqual(failed, [], f"核心模块导入失败：{failed}")

    def test_downloader_does_not_import_gui(self):
        import app.core.downloader as dl
        mods = {m for m in sys.modules}
        for name in ("customtkinter", "tkinter"):
            if name in mods:
                # 只要 downloader 自己没有引用即可
                src = Path(dl.__file__).read_text(encoding="utf-8")
                self.assertNotIn(f"import {name}", src)


class TestImportsGui(unittest.TestCase):
    """UI 模块用桩依赖导入：抓导入期错误、模块级表达式错误。"""

    @classmethod
    def setUpClass(cls):
        stubs.install()

    def test_ui_modules_import(self):
        import app
        failed = []
        count = 0
        for m in pkgutil.walk_packages(app.__path__, "app."):
            if ".ui" not in m.name:
                continue
            try:
                importlib.import_module(m.name)
                count += 1
            except Exception as e:  # noqa: BLE001
                failed.append((m.name, f"{type(e).__name__}: {e}"))
        self.assertGreater(count, 20, "UI 模块数量异常")
        self.assertEqual(failed, [], f"UI 模块导入失败：{failed}")

    def test_main_module_import(self):
        mod = importlib.import_module("main")
        self.assertTrue(hasattr(mod, "main"))
        self.assertTrue(hasattr(mod, "_pick_app_base"))
        base, dnd = mod._pick_app_base()
        self.assertTrue(inspect.isclass(base))
        self.assertIsInstance(dnd, bool)


class TestStartupChain(unittest.TestCase):
    """启动链路契约：main.py 依赖的那些入口必须存在且可组合。"""

    @classmethod
    def setUpClass(cls):
        stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-startup-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def test_window_class_can_be_composed(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        WindowClass = type("WindowClass", (MainWindow, AppBase), {})
        self.assertTrue(issubclass(WindowClass, MainWindow))

    def test_mainwindow_required_hooks(self):
        from app.ui.main_window import MainWindow
        # withdraw / deiconify / mainloop 由 Tk 基类提供，这里只查 MainWindow 自己的
        for name in ("prepare_offscreen_warmup", "on_boot_done",
                     "get_boot_tasks"):
            self.assertTrue(hasattr(MainWindow, name),
                            f"MainWindow 缺少 {name}")

    def test_splash_signature(self):
        from app.ui.splash import SplashScreen
        sig = inspect.signature(SplashScreen.__init__)
        self.assertIn("on_done", sig.parameters)
        self.assertIn("boot_tasks", sig.parameters)

    def test_boot_tasks_shape(self):
        """boot_tasks 需要是可迭代的 (title, callable) 序列。"""
        src = (_ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("def get_boot_tasks", src)
        self.assertIn("def on_boot_done", src)

    def test_window_constructs_and_boots_under_stubs(self):
        """
        用桩模块把 main.py 的启动链路真正跑一遍：
            组类 → 构造主窗口 → prepare_offscreen_warmup（双端预热）
            → get_boot_tasks → 逐个跑 boot 任务
        能抓出构造期 / 预热期 / 引导期的 Python 级错误（NameError、
        AttributeError、类型错误等）。注意：**不覆盖真实 Tk 渲染**。
        """
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        WindowClass = type("WindowClass", (MainWindow, AppBase), {})
        app = WindowClass()
        app.prepare_offscreen_warmup()
        tasks = app.get_boot_tasks()
        self.assertIsInstance(tasks, list)
        self.assertGreaterEqual(len(tasks), 3)
        for item in tasks:
            title, fn = item
            self.assertIsInstance(title, str)
            self.assertTrue(callable(fn), f"boot 任务 {title} 不可调用")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.fail(f"boot 任务「{title}」抛异常："
                          f"{type(e).__name__}: {e}")


class TestUiContract(unittest.TestCase):
    """player_view / slot_panel / downloader 三者之间的接口契约。"""

    @classmethod
    def setUpClass(cls):
        stubs.install()

    def test_slot_panel_signature(self):
        from app.ui.widgets.slot_panel import SlotPanel
        sig = inspect.signature(SlotPanel.__init__)
        self.assertIn("slots", sig.parameters)
        self.assertIn("on_toggle", sig.parameters)
        for name in ("set_slots", "slot_count", "update"):
            self.assertTrue(hasattr(SlotPanel, name),
                            f"SlotPanel 缺少 {name}")

    def test_player_view_slot_bridge(self):
        from app.ui.player_view import PlayerView
        for name in ("_toggle_slot_panel", "_on_slot_toggle",
                     "_do_slot_refresh", "_on_byte_progress",
                     "_update_download_progress", "_on_download_done"):
            self.assertTrue(hasattr(PlayerView, name),
                            f"PlayerView 缺少 {name}")
        sig = inspect.signature(PlayerView._on_slot_toggle)
        self.assertEqual(list(sig.parameters), ["self", "expand"])

    def test_player_view_calls_download_files_with_expected_kwargs(self):
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn("from ..core.downloader import DownloadTask, download_files",
                      src)
        for kwarg in ("target_dir=", "threads=", "progress=", "log=",
                      "byte_progress=", "should_abort=",
                      "on_file_started=", "on_file_done=",
                      "on_file_failed="):
            self.assertIn(kwarg, src, f"player_view 没有传 {kwarg}")

    def test_player_view_result_semantics(self):
        """
        调用方按 `成功 = len(results) - len(failed)` 统计，并靠
        `on_file_done`（**只在成功时触发**）收集已完成文件；
        返回列表里每个任务恰好一条结果（成功也要返回）。
        """
        src = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        self.assertIn("self._mark_completed(task.rel_path)", src)
        self.assertIn("def _mark_completed", src)
        self.assertIn("if r.ok:", src)
        self.assertIn("len(results) - len(failed)", src)
        self.assertIn("failed = [r for r in results if not r.ok and not r.aborted]",
                      src)

    def test_slot_panel_uses_on_toggle(self):
        src = (_ROOT / "app" / "ui" / "widgets" / "slot_panel.py").read_text(
            encoding="utf-8")
        self.assertIn("self._on_toggle(False)", src)
        self.assertIn("on_toggle", src)


class TestDownloadPanel(unittest.TestCase):
    """「下不下来的文件交还给用户」这块的契约与匹配逻辑。"""

    @classmethod
    def setUpClass(cls):
        stubs.install()

    def _panel(self, items, drop_handler=None, notice=None):
        from app.ui.widgets.download_panel import DownloadPanel
        calls = []

        def handler(rel, path):
            calls.append((rel, str(path)))
            if drop_handler is None:
                return True
            return drop_handler(rel, path)

        panel = DownloadPanel(None, failed_items=items,
                              on_file_dropped=handler,
                              on_skip_all=lambda: None,
                              in_progress=False,
                              on_notice=notice)
        return panel, calls

    @staticmethod
    def _item(rel, urls, error="boom"):
        return {"rel": rel, "urls": list(urls), "error": error}

    # -- 名称归一化 -----------------------------------------------------
    def test_normalize_keeps_version_numbers(self):
        from app.ui.widgets.download_panel import DownloadPanel as P
        self.assertEqual(P._normalize_name("create-1.20.1-6.0.7.jar"),
                         "create-1.20.1-6.0.7.jar")
        self.assertEqual(P._normalize_name("jei-1.21.1-fabric-19.57.0.451.jar"),
                         "jei-1.21.1-fabric-19.57.0.451.jar")

    def test_normalize_strips_browser_copy_suffix(self):
        from app.ui.widgets.download_panel import DownloadPanel as P
        want = "create-1.20.1-6.0.7.jar"
        for name in ("create-1.20.1-6.0.7 (1).jar",
                     "create-1.20.1-6.0.7(2).jar",
                     "create-1.20.1-6.0.7 - 副本.jar",
                     "create-1.20.1-6.0.7 - copy.jar",
                     "Create-1.20.1-6.0.7.JAR"):
            self.assertEqual(P._normalize_name(name), want, name)

    # -- 链接 -----------------------------------------------------------
    def test_links_of_returns_all_sources(self):
        panel, _ = self._panel([self._item(
            "mods/a.jar", ["https://a/1.jar", "https://b/2.jar",
                           "https://a/1.jar", ""])])
        self.assertEqual(panel.links_of("mods/a.jar"),
                         ["https://a/1.jar", "https://b/2.jar"])

    def test_links_text_has_filename_and_all_sources(self):
        panel, _ = self._panel([self._item(
            "mods/a.jar", ["https://a/1.jar", "https://mirror/2.jar"])])
        text = panel.links_text("mods/a.jar")
        self.assertTrue(text.startswith("# a.jar"))
        self.assertIn("https://a/1.jar", text)
        self.assertIn("https://mirror/2.jar", text)

    def test_panel_exposes_copy_and_open(self):
        from app.ui.widgets.download_panel import DownloadPanel
        for name in ("_copy_links", "_open_link", "links_of", "links_text",
                     "_candidates", "add_item", "has_failures"):
            self.assertTrue(hasattr(DownloadPanel, name),
                            f"DownloadPanel 缺少 {name}")
        sig = inspect.signature(DownloadPanel.__init__)
        self.assertIn("on_notice", sig.parameters)

    # -- 拖入匹配 -------------------------------------------------------
    def test_candidates_exact_and_fuzzy(self):
        panel, _ = self._panel([self._item("mods/foo-1.0.jar", ["u"])])
        self.assertEqual(panel._candidates("foo-1.0.jar"), ["mods/foo-1.0.jar"])
        self.assertEqual(panel._candidates("FOO-1.0.JAR"), ["mods/foo-1.0.jar"])
        self.assertEqual(panel._candidates("foo-1.0 (1).jar"),
                         ["mods/foo-1.0.jar"])

    def test_candidates_single_remaining_fallback(self):
        panel, _ = self._panel([self._item("mods/foo-1.0.jar", ["u"])])
        self.assertEqual(panel._candidates("完全不相干的名字.jar"),
                         ["mods/foo-1.0.jar"])

    def test_candidates_ambiguous_returns_none_path(self):
        panel, _ = self._panel([
            self._item("mods/a.jar", ["u"]),
            self._item("mods/b.jar", ["u"]),
        ])
        self.assertEqual(panel._candidates("c.jar"), [])

    def test_same_basename_in_two_folders_tries_both(self):
        panel, calls = self._panel([
            self._item("mods/x.jar", ["u"]),
            self._item("resourcepacks/x.jar", ["u"]),
        ], drop_handler=lambda rel, path: rel == "resourcepacks/x.jar")
        panel._on_files_dropped([Path("x.jar")])
        self.assertEqual([c[0] for c in calls],
                         ["mods/x.jar", "resourcepacks/x.jar"])
        self.assertTrue(panel.has_failures(), "只该移除校验通过的那一项")
        self.assertNotIn("resourcepacks/x.jar", panel._failed)

    # -- 拖入结果反馈 ---------------------------------------------------
    def test_drop_success_removes_item(self):
        notices = []
        panel, calls = self._panel(
            [self._item("mods/a.jar", ["u"])],
            notice=lambda lv, t: notices.append((lv, t)))
        panel._on_files_dropped([Path("/tmp/a.jar")])
        self.assertEqual(calls, [("mods/a.jar", "/tmp/a.jar")])
        self.assertFalse(panel.has_failures())

    def test_drop_mismatch_reports(self):
        notices = []
        panel, calls = self._panel(
            [self._item("mods/a.jar", ["u"]),
             self._item("mods/b.jar", ["u"])],
            notice=lambda lv, t: notices.append((lv, t)))
        panel._on_files_dropped([Path("/tmp/zzz.jar")])
        self.assertEqual(calls, [])
        self.assertTrue(any("不在待补入列表" in t for _lv, t in notices),
                        f"没有给出提示：{notices}")
        self.assertTrue(panel.has_failures())

    def test_drop_verify_failure_reports(self):
        notices = []
        panel, calls = self._panel(
            [self._item("mods/a.jar", ["u"])],
            drop_handler=lambda rel, path: False,
            notice=lambda lv, t: notices.append((lv, t)))
        panel._on_files_dropped([Path("/tmp/a.jar")])
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("校验未通过" in t for _lv, t in notices),
                        f"没有给出提示：{notices}")
        self.assertTrue(panel.has_failures(), "校验不过的项应当留着继续让用户试")


class TestConfigConsistency(unittest.TestCase):
    """配置三处（downloader._DEFAULTS / database 默认值 / 首选项类型）不许漂移。"""

    @classmethod
    def setUpClass(cls):
        stubs.install()
        from app.core import database as db
        cls.db = db
        from app.core import downloader as dl
        cls.dl = dl
        # 程序配置目录 / 数据库目录 / 最近列表全部指到临时目录，
        # 测试绝不写工作区外的真实文件
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-cfg-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        cls._dbdir = Path(cls._tmp.name) / "db"
        cls._dbdir.mkdir(parents=True, exist_ok=True)
        db.set_db_path(cls._dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    # 原项目既有差异（本次不做行为变更，仅记录）
    KNOWN_PREEXISTING_DIFFS = {"multi_slots"}

    def test_defaults_match_database(self):
        dl_def = self.dl._DEFAULTS
        db_def = self.db.DEFAULT_DOWNLOAD_OPTIONS
        for key, value in dl_def.items():
            self.assertIn(key, db_def,
                          f"database.DEFAULT_DOWNLOAD_OPTIONS 缺少 {key}")
            if key in self.KNOWN_PREEXISTING_DIFFS:
                continue
            self.assertEqual(db_def[key], value,
                             f"{key} 默认值不一致："
                             f"downloader={value!r} database={db_def[key]!r}")

    def test_meta_keys_exist(self):
        for key, _label, _desc, _eg in self.db.DOWNLOAD_OPTION_META:
            self.assertIn(key, self.db.DEFAULT_DOWNLOAD_OPTIONS,
                          f"首选项里的 {key} 不在默认配置中")

    def test_preferences_types_cover_new_keys(self):
        from app.ui.widgets import preferences_dialog as pd
        bad = []
        for key, _label, _desc, _eg in self.db.DOWNLOAD_OPTION_META:
            default = self.db.DEFAULT_DOWNLOAD_OPTIONS.get(key)
            raw = str(default)
            try:
                if key in pd._INT_KEYS:
                    got = int(raw)
                elif key in pd._FLOAT_KEYS:
                    got = float(raw)
                else:
                    got = raw
            except (TypeError, ValueError):
                bad.append((key, default))
                continue
            if isinstance(default, bool):
                continue
            if isinstance(default, int) and key not in pd._INT_KEYS:
                bad.append((key, "整数未登记到 _INT_KEYS"))
            if isinstance(default, float) and key not in pd._FLOAT_KEYS:
                bad.append((key, "浮点未登记到 _FLOAT_KEYS"))
            if not isinstance(default, (int, float)) and got is None:
                bad.append((key, "取值失败"))
        self.assertEqual(bad, [], f"首选项类型登记不一致：{bad}")

    def test_get_download_options_contains_all_defaults(self):
        opts = self.db.get_download_options()
        for key in self.dl._DEFAULTS:
            self.assertIn(key, opts, f"get_download_options 缺少 {key}")

    def test_set_and_reload_roundtrip(self):
        self.db.set_download_options({"multi_slots": 6,
                                      "read_poll_interval": 0.5,
                                      "part_meta_enabled": 0})
        opts = self.db.get_download_options()
        self.assertEqual(opts["multi_slots"], 6)
        self.assertEqual(opts["read_poll_interval"], 0.5)
        self.assertEqual(opts["part_meta_enabled"], 0)
        # 未修改的键必须保留
        self.assertEqual(opts["single_slots"],
                         self.db.DEFAULT_DOWNLOAD_OPTIONS["single_slots"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

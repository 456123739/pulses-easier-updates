"""
test_fixes.py — 流程审查修复的回归测试（第一批：止血）
------------------------------------------------
每个用例都对应审查报告里的一个 P0：

  P0-1  非白名单目录被默认"完全匹配" → rmtree 玩家本地目录（且不回滚）
  P0-2  更新包无 overrides/ 时元数据被写进整合包根目录
  P0-3  下载阶段没有锁 → 处理中可换包/清空
  P0-4  「全部跳过」在下载进行中就可点 → 两个 execute_plan 并发
  P0-5  部分失败后没有终态 → 补入全部成功也没有出口
  P0-6  「无需下载直接应用」不装关闭守卫

运行：  python3 tests/test_fixes.py
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                     # noqa: E402


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Res:
    """download_files 的结果对象替身"""

    def __init__(self, rel: str, ok: bool = True, aborted: bool = False):
        self.ok = ok
        self.aborted = aborted
        self.task = types.SimpleNamespace(rel_path=rel)


def _install_stubs():
    if "customtkinter" not in sys.modules:
        stubs.install()
    else:
        stubs.install()


# ======================================================================
# P0-1  目录比对 / 目录替换
# ======================================================================
class TestFolderDiffIgnoresMtime(unittest.TestCase):
    """folder_hash 不能把 mtime 算进去（解压包的 mtime 恒为"现在"）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-diff-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_identical_content_different_mtime_is_same(self):
        from app.core.differ import folder_hash, folders_differ
        a = self.tmp / "a"
        b = self.tmp / "b"
        _write(a / "x.toml", "a=1")
        _write(b / "x.toml", "a=1")
        import os
        os.utime(b / "x.toml", (1, 1))          # 制造 mtime 差异
        os.utime(a / "x.toml", (2_000_000_000, 2_000_000_000))
        self.assertEqual(folder_hash(a), folder_hash(b))
        self.assertFalse(folders_differ(a, b))

    def test_same_size_different_content_is_different(self):
        from app.core.differ import folders_differ
        a = self.tmp / "a"
        b = self.tmp / "b"
        _write(a / "x.toml", "a=1")
        _write(b / "x.toml", "a=2")             # 同长度、内容不同
        self.assertTrue(folders_differ(a, b))

    def test_added_and_removed_files_detected(self):
        from app.core.differ import folders_differ
        a = self.tmp / "a"
        b = self.tmp / "b"
        _write(a / "x", "1")
        _write(a / "y", "2")
        _write(b / "x", "1")
        self.assertTrue(folders_differ(a, b))

    def test_diff_does_not_flag_identical_dir(self):
        """文件集合与内容都相同、只有 mtime 不同 → 不该判 MODIFIED。"""
        from app.core.differ import ChangeKind, diff_packs_parallel
        import os
        old = self.tmp / "instance"
        new = self.tmp / "overrides"
        _write(old / "config" / "shipped.toml", "a=1")
        _write(new / "config" / "shipped.toml", "a=1")
        os.utime(new / "config" / "shipped.toml", (1, 1))
        diff = diff_packs_parallel(old, new, whitelist=["mods"],
                                   index_hashes={})
        kinds = [(c.rel_path.as_posix(), c.kind) for c in diff.modified]
        self.assertNotIn(("config", ChangeKind.MODIFIED), kinds,
                         "内容相同却判了 MODIFIED（mtime 又参与了判定？）")

    def test_diff_flags_pack_superset_dir(self):
        """
        玩家目录里有更新包没带的文件 → 判 MODIFIED 是对的；
        真正要保证的是"应用时不能删掉玩家那些文件"（见 e2e 用例）。
        """
        from app.core.differ import ChangeKind, diff_packs_parallel
        old = self.tmp / "instance2"
        new = self.tmp / "overrides2"
        _write(old / "config" / "shipped.toml", "a=1")
        _write(old / "config" / "user-only.toml", "b=2")
        _write(new / "config" / "shipped.toml", "a=1")
        diff = diff_packs_parallel(old, new, whitelist=["mods"],
                                   index_hashes={})
        kinds = [(c.rel_path.as_posix(), c.kind) for c in diff.modified]
        self.assertIn(("config", ChangeKind.MODIFIED), kinds)


class TestSafeDirReplace(unittest.TestCase):
    """FULL_MATCH 目录替换：失败要能**事务性**回滚，成功不留备份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-repl-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self):
        msgs = []
        return msgs, (lambda level, msg: msgs.append((level, msg)))

    def test_full_replace_keeps_no_backup_on_success(self):
        from app.core.updater import _replace_dir_full
        old = self.tmp / "instance"
        new = self.tmp / "new"
        _write(old / "config" / "user-only.toml", "gone")
        _write(new / "config" / "shipped.toml", "a=1")

        _msgs, log = self._log()
        ok = _replace_dir_full(new / "config", old / "config", log)
        self.assertTrue(ok)
        self.assertTrue((old / "config" / "shipped.toml").is_file())
        self.assertFalse((old / "config" / "user-only.toml").exists())
        # 成功路径不留任何备份/回收站
        leftovers = [p.name for p in (old / "config").parent.iterdir()
                     if p.name.startswith(".") and "pulses" in p.name]
        self.assertEqual(leftovers, [])

    def test_full_replace_rolls_back_on_copy_failure(self):
        from app.core import updater
        old = self.tmp / "instance"
        new = self.tmp / "new"
        _write(old / "config" / "shipped.toml", "old")
        _write(old / "config" / "user-only.toml", "keep")
        _write(new / "config" / "shipped.toml", "new")

        _msgs, log = self._log()
        shim = types.SimpleNamespace(
            copytree=mock.Mock(side_effect=OSError("disk full")),
            rmtree=updater.shutil.rmtree,
            copy2=updater.shutil.copy2,
        )
        with mock.patch.object(updater, "shutil", shim):
            ok = updater._replace_dir_full(new / "config", old / "config", log)

        self.assertFalse(ok)
        self.assertEqual((old / "config" / "shipped.toml").read_text(), "old")
        self.assertEqual((old / "config" / "user-only.toml").read_text(),
                         "keep")
        # 回滚后不留临时目录
        leftovers = [p.name for p in old.iterdir()
                     if p.name.startswith(".") and "pulses" in p.name]
        self.assertEqual(leftovers, [])


# ======================================================================
# P0-2  跨端契约：overrides/ 与保留名
# ======================================================================
class TestPackContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-pack-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        from app.core import database as db
        cls.db = db
        cls.dbdir = Path(cls._tmp.name) / "db"
        cls.dbdir.mkdir(parents=True, exist_ok=True)
        db.set_db_path(cls.dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def test_export_without_overrides_still_has_overrides_entry(self):
        import json
        import zipfile
        from app.core import eapack
        from app.core.mrpack import MRPack, OVERRIDES_DIR
        with tempfile.TemporaryDirectory(prefix="easier-exp-") as td:
            tmp = Path(td)
            src = tmp / "src"
            src.mkdir()
            index = {"formatVersion": 1, "name": "t", "versionId": "1",
                     "files": []}
            (src / "modrinth.index.json").write_text(
                json.dumps(index), encoding="utf-8")
            zip_path = tmp / "src.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("modrinth.index.json", json.dumps(index))
            out = tmp / "out.eapack"
            res = eapack.export_eapack(
                source_dir=src, source_zip=zip_path, out_path=out,
                pack=MRPack(name="t"), merged_files=[], strategies={},
                changelog_md="")
            self.assertIsNotNone(res)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            self.assertIn(OVERRIDES_DIR + "/", names)

    def test_reserved_root_names_detected(self):
        from app.core.mrpack import is_reserved_root_name
        for n in ("modrinth.index.json", "ea_settings.json",
                  "ea_hashes.json", "ea_manifest.json", "changelog.md",
                  "Modrinth.Index.JSON"):
            self.assertTrue(is_reserved_root_name(n), n)
        self.assertFalse(is_reserved_root_name("options.txt"))
        self.assertFalse(is_reserved_root_name("servers.dat"))

    def test_root_file_items_skips_reserved(self):
        from app.ui.player_view import PlayerView
        with tempfile.TemporaryDirectory(prefix="easier-rf-") as td:
            root = Path(td)
            for n in ("modrinth.index.json", "ea_settings.json",
                      "ea_manifest.json", "options.txt"):
                _write(root / n, "x")
            items = PlayerView._root_file_items(root)
            self.assertEqual(items, {"options.txt"})

    def test_merged_source_skips_reserved_in_fallback(self):
        from app.ui.player_view import PlayerView
        with tempfile.TemporaryDirectory(prefix="easier-ms-") as td:
            tmp = Path(td)
            view = PlayerView.__new__(PlayerView)
            view._temp_dir = types.SimpleNamespace(name=str(tmp / "work"))
            (tmp / "work").mkdir(parents=True, exist_ok=True)
            view._cache_root = tmp / "cache"
            view._cache_root.mkdir()
            root = tmp / "extract"
            _write(root / "modrinth.index.json", "{}")
            _write(root / "ea_manifest.json", "{}")
            _write(root / "options.txt", "fov:90")
            _write(root / "mods" / "a.jar", "J")
            view._overrides_root = root
            view._overrides_fallback = True
            view.log = types.SimpleNamespace(log=lambda *a, **k: None)
            merged = view._build_merged_source()
            self.assertIsNotNone(merged)
            got = sorted(p.relative_to(merged).as_posix()
                         for p in merged.rglob("*") if p.is_file())
            self.assertEqual(got, ["mods/a.jar", "options.txt"])

    def test_merged_source_keeps_legit_overrides_root_changelog(self):
        """真正的 overrides/ 里的 changelog.md 是内容，不能被当成元数据。"""
        from app.ui.player_view import PlayerView
        with tempfile.TemporaryDirectory(prefix="easier-ms2-") as td:
            tmp = Path(td)
            view = PlayerView.__new__(PlayerView)
            view._temp_dir = types.SimpleNamespace(name=str(tmp / "work"))
            (tmp / "work").mkdir(parents=True, exist_ok=True)
            view._cache_root = tmp / "cache"
            view._cache_root.mkdir()
            root = tmp / "extract" / "overrides"
            _write(root / "changelog.md", "real content")
            view._overrides_root = root
            view._overrides_fallback = False
            view.log = types.SimpleNamespace(log=lambda *a, **k: None)
            merged = view._build_merged_source()
            self.assertTrue((merged / "changelog.md").is_file())


# ======================================================================
# P0-3 / P0-4 / P0-5 / P0-6  状态机
# ======================================================================
class TestPlayerStateMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-flow-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        WindowClass = type("WindowClass", (MainWindow, AppBase), {})
        self.app = WindowClass()
        self.view = self.app.player_view
        self.pv = self.view

    # -- P0-3 -----------------------------------------------------------
    def test_drop_pack_rejected_while_downloading(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        before = self.pv.update_zip
        self.pv._on_update_pack_dropped([Path("/tmp/whatever.zip")])
        self.assertIs(self.pv.update_zip, before)

    def test_drop_pack_rejected_while_applying(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_APPLY
        before = self.pv.update_zip
        self.pv._on_update_pack_dropped([Path("/tmp/whatever.zip")])
        self.assertIs(self.pv.update_zip, before)

    def test_clear_rejected_while_busy(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        self.pv.update_zip = Path("/tmp/keep.zip")
        self.pv._on_clear_pack_click()
        self.assertEqual(self.pv.update_zip, Path("/tmp/keep.zip"))

    def test_download_phase_locks_app_state(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_IDLE
        self.pv._download_tasks = [types.SimpleNamespace(rel_path="a")]
        self.pv._cache_root = Path(self._tmp.name) / "cache"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(pv, "download_files",
                               return_value=[_Res("a")]):
            self.pv._start_download_and_apply()
        self.assertEqual(self.pv._phase, pv._PHASE_DOWNLOAD)
        self.assertTrue(self.app.app_state.is_locked)

    # -- P0-4 -----------------------------------------------------------
    def test_start_apply_is_reentrant_safe(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_APPLY
        with mock.patch.object(pv, "execute_plan") as ex:
            self.pv._start_apply()
        ex.assert_not_called()

    def test_start_apply_refuses_while_downloading(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        with mock.patch.object(pv, "execute_plan") as ex:
            self.pv._start_apply()
        ex.assert_not_called()

    def test_give_up_applies_immediately_when_download_finished(self):
        """「放弃」：下载已结束 → 立刻应用（并会把缺失写进凭证）。"""
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_MANUAL
        self.pv._cache_root = Path(self._tmp.name) / "cache2"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)
        self.pv._download_tasks = [types.SimpleNamespace(rel_path="a")]
        self.pv._pending_errors = {"a": "boom"}
        thread = mock.Mock()
        thread.is_alive.return_value = False
        self.pv._download_thread = thread

        started = []
        with mock.patch.object(self.pv, "_start_apply",
                               side_effect=lambda: started.append(1)):
            self.pv._do_give_up(True)
        self.assertEqual(started, [1])
        self.assertTrue(self.pv._give_up)
        self.assertEqual(self.pv._phase, pv._PHASE_IDLE)

    def test_give_up_waits_for_remaining_downloads(self):
        """「放弃」不中止其余下载：还有文件在下时先等下载收尾。"""
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        self.pv._cache_root = Path(self._tmp.name) / "cache2b"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)
        self.pv._download_tasks = [types.SimpleNamespace(rel_path="a")]
        self.pv._pending_errors = {"a": "boom"}

        class _Alive:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                return None

        self.pv._download_thread = _Alive()
        started = []
        with mock.patch.object(self.pv, "_start_apply",
                               side_effect=lambda: started.append(1)):
            self.pv._do_give_up(True)
            self.assertEqual(started, [], "还有下载在跑时不该立刻应用")
            self.assertTrue(self.pv._give_up)
            self.assertFalse(self.pv._abort_flag, "放弃不该中止其余下载")
            # 下载收尾后由 _on_download_done 接手应用
            self.pv._phase = pv._PHASE_DOWNLOAD
            self.pv._on_download_done([_Res("a", ok=False)])
        self.assertEqual(started, [1])

    def test_download_done_does_not_double_apply(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_APPLY
        with mock.patch.object(self.pv, "_start_apply") as ap:
            self.pv._on_download_done([_Res("a")])
        ap.assert_not_called()

    # -- P0-5 -----------------------------------------------------------
    def test_failure_branch_enters_manual_state(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        self.pv._download_tasks = [types.SimpleNamespace(
            rel_path="a", urls=["u"], sha1="", sha256="", sha512="",
            file_size=0), types.SimpleNamespace(
            rel_path="b", urls=["u"], sha1="", sha256="", sha512="",
            file_size=0)]
        self.pv._cache_root = Path(self._tmp.name) / "cache3"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)
        # 失败事件先把这一项记为"待补入"
        self.pv._handle_file_failed("b", ["u"], "boom")
        self.pv._on_download_done([_Res("a"), _Res("b", ok=False)])
        self.assertEqual(self.pv._phase, pv._PHASE_MANUAL)
        self.assertTrue(self.pv.pending_banner is not None,
                        "受阻时应亮出提示条")

    def test_all_resolved_continues_to_apply(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_MANUAL
        started = []
        with mock.patch.object(self.pv, "_start_apply",
                               side_effect=lambda: started.append(1)):
            self.pv._on_all_failures_resolved()
        self.assertEqual(started, [1])
        self.assertEqual(self.pv._phase, pv._PHASE_IDLE)

    def test_pending_window_fires_on_all_resolved(self):
        from app.ui.widgets.pending_window import PendingWindow
        fired = []
        win = PendingWindow(
            None, mode="wait",
            items=[{"rel": "mods/a.jar", "urls": ["u"], "error": ""}],
            on_file_dropped=lambda rel, path: True,
            on_all_resolved=lambda: fired.append(1))
        win._handle_drop("mods/a.jar", [Path("/tmp/a.jar")])
        self.assertFalse(win.has_pending())
        self.assertEqual(fired, [1])

    def test_pending_window_does_not_fire_while_items_remain(self):
        from app.ui.widgets.pending_window import PendingWindow
        fired = []
        win = PendingWindow(
            None, mode="wait",
            items=[{"rel": "mods/a.jar", "urls": ["u"], "error": ""},
                   {"rel": "mods/b.jar", "urls": ["u"], "error": ""}],
            on_file_dropped=lambda rel, path: rel == "mods/a.jar",
            on_all_resolved=lambda: fired.append(1))
        win._handle_drop("mods/a.jar", [Path("/tmp/a.jar")])
        self.assertEqual(fired, [])

    # -- P0-6 -----------------------------------------------------------
    def test_apply_installs_close_guard(self):
        import app.ui.player_view as pv
        from app.core.differ import DiffResult
        self.pv._phase = pv._PHASE_IDLE
        self.pv.diff = DiffResult()
        self.pv.pack_info = types.SimpleNamespace(path=Path(self._tmp.name))
        self.pv._overrides_root = Path(self._tmp.name)
        self.pv._temp_dir = types.SimpleNamespace(name=self._tmp.name)
        self.pv._cache_root = Path(self._tmp.name) / "cache4"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)

        installed = []
        with mock.patch.object(self.pv, "_install_close_guard",
                               side_effect=lambda: installed.append(1)), \
                mock.patch.object(pv, "execute_plan",
                                  return_value={"applied": 0, "deleted": 0,
                                                "skipped": 0,
                                                "failed": []}):
            self.pv._start_apply()
        self.assertEqual(installed, [1], "应用阶段没有安装关闭守卫")
        self.assertEqual(self.pv._phase, pv._PHASE_APPLY)

    # -- 关闭时不要和下载线程抢 .part ------------------------------------
    def test_close_skips_part_cleanup_when_thread_alive(self):
        import app.ui.player_view as pv
        self.pv._phase = pv._PHASE_DOWNLOAD
        self.pv._cache_root = Path(self._tmp.name) / "cache5"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)

        class _AliveThread:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                return None

        self.pv._download_thread = _AliveThread()
        cleaned = []

        class _SyncThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target

            def start(self):
                self._target()

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        with mock.patch.object(pv.threading, "Thread", _SyncThread), \
                mock.patch.object(pv.resume_mod, "cleanup_stale_parts",
                                  side_effect=lambda p: cleaned.append(p)):
            self.pv._handle_close_download(True)
        self.assertEqual(cleaned, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

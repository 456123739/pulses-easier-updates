"""
test_transfer.py — 批次1「应用阶段搬运」回归测试
------------------------------------------------
对应这一批修的四个问题：

  T1  应用前先在 UI 线程把整包复制成 _merged 副本 → 窗口冻住
      （现在按需取源，并且计划/搬运/校验全在后台线程）
  T2  文件级写入不是原子的 → 强杀/断电在整合包里留下半截文件
      （现在一律"临时名 + os.replace"）
  T3  搬运用复制而不是移动 → 同一份数据读写两遍
      （同卷 rename/硬链接是元数据操作，不产生 IO）
  T4  应用完成后状态不复位 → 变更列表过期、再点一次会重跑一遍

运行：  python3 tests/test_transfer.py
"""

import hashlib
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs

from app.config import ChangeKind, Strategy
from app.core import transfer as T
from app.core.updater import (
    PlanSource,
    SourceLayer,
    UpdatePlan,
    build_plan,
    execute_plan,
    verify_after_update,
)


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class _FakeXDev:
    """把"源 → 目标"这一步变成跨卷失败（模拟缓存盘 ≠ 整合包盘）。"""

    def __init__(self, also_link: bool = False):
        self.also_link = also_link
        self._replace = os.replace
        self._link = os.link

    def __enter__(self):
        def _replace(a, b, *args, **kw):
            if ".pulses_new" not in str(a):
                raise OSError(18, "Invalid cross-device link")
            return self._replace(a, b, *args, **kw)

        def _link(a, b, *args, **kw):
            raise OSError(1, "Operation not permitted")

        os.replace = _replace
        if self.also_link:
            os.link = _link
        return self

    def __exit__(self, *exc):
        os.replace = self._replace
        os.link = self._link
        return False


# ======================================================================
# T2 / T3  transfer.move_in：原子 + 移动
# ======================================================================
class TestMoveIn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-tr-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_volume_move_is_instant_and_consumes_source(self):
        data = os.urandom(300_000)
        src = self.tmp / "cache" / "a.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(data)
        dst = self.tmp / "inst" / "mods" / "a.jar"

        res = T.move_in(src, dst, expect_size=len(data),
                        expect_hash=_sha1(data))
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.moved, "同卷应当是瞬时移动")
        self.assertEqual(res.bytes_copied, 0)
        self.assertFalse(src.exists(), "可消耗的源应当被移走")
        self.assertEqual(dst.read_bytes(), data)

    def test_preserve_source_uses_hardlink(self):
        data = os.urandom(100_000)
        src = self.tmp / "cache" / "b.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(data)
        dst = self.tmp / "inst" / "mods" / "b.jar"

        res = T.move_in(src, dst, preserve=True)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.moved)
        self.assertTrue(src.exists(), "缓存必须保留（方便重试/续传）")
        self.assertEqual(dst.read_bytes(), data)
        if hasattr(os, "stat"):
            self.assertEqual(os.stat(src).st_ino, os.stat(dst).st_ino)

    def test_cross_volume_falls_back_to_chunked_copy(self):
        data = os.urandom(1024 * 300)
        src = self.tmp / "cache" / "c.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(data)
        dst = self.tmp / "inst" / "mods" / "c.jar"

        with _FakeXDev(also_link=True):
            res = T.move_in(src, dst, expect_size=len(data),
                            expect_hash=_sha1(data), chunk_bytes=64 * 1024,
                            pace_ms=0.001)
        self.assertTrue(res.ok, res.error)
        self.assertFalse(res.moved, "跨卷必须真的搬字节")
        self.assertEqual(res.bytes_copied, len(data))
        self.assertEqual(res.digest, _sha1(data))
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_bytes(), data)
        self.assertEqual(list(dst.parent.glob(".*pulses_new")), [])

    def test_cross_volume_keeps_source_when_preserve(self):
        data = os.urandom(200_000)
        src = self.tmp / "cache" / "d.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(data)
        dst = self.tmp / "inst" / "mods" / "d.jar"

        with _FakeXDev(also_link=True):
            res = T.move_in(src, dst, preserve=True, expect_size=len(data))
        self.assertTrue(res.ok, res.error)
        self.assertTrue(src.exists())
        self.assertEqual(dst.read_bytes(), data)

    def test_verify_failure_keeps_old_target_intact(self):
        """内容校验失败 → 目标端**旧文件一字不改**、不留临时文件。"""
        old = b"OLD-GOOD-CONTENT" * 100
        new = os.urandom(4096)
        src = self.tmp / "cache" / "e.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(new)
        dst = self.tmp / "inst" / "mods" / "e.jar"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(old)

        with _FakeXDev(also_link=True):
            res = T.move_in(src, dst, expect_hash="0" * 40)
        self.assertFalse(res.ok)
        self.assertIn("内容校验失败", res.error)
        self.assertEqual(dst.read_bytes(), old, "旧文件必须完好无损")
        self.assertEqual(list(dst.parent.glob(".*pulses_new")), [])
        self.assertTrue(src.exists(), "失败不许消耗源")

    def test_size_mismatch_refused_without_touching_target(self):
        src = self.tmp / "cache" / "f.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"x" * 10)
        dst = self.tmp / "inst" / "mods" / "f.jar"
        res = T.move_in(src, dst, expect_size=11)
        self.assertFalse(res.ok)
        self.assertFalse(dst.exists())
        self.assertTrue(src.exists())

    def test_locked_target_gives_readable_error(self):
        src = self.tmp / "cache" / "g.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"x" * 10)
        dst = self.tmp / "inst" / "mods" / "g.jar"
        dst.parent.mkdir(parents=True)

        def _boom(a, b, *args, **kw):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(os, "replace", _boom):
            res = T.move_in(src, dst)
        self.assertFalse(res.ok)
        self.assertIn("被占用", res.error)
        self.assertIn("关闭游戏", res.error)

    def test_partial_write_never_visible_at_target(self):
        """
        模拟"复制到一半进程没了"：临时文件被留下，但目标端那个文件
        仍然是**旧的完整文件**（这正是原子替换要保证的）。
        """
        old = b"OLD" * 1000
        src = self.tmp / "cache" / "h.jar"
        src.parent.mkdir(parents=True)
        src.write_bytes(os.urandom(500_000))
        dst = self.tmp / "inst" / "mods" / "h.jar"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(old)

        real_replace = T.__dict__["os"].replace

        def _die(a, b, *args, **kw):
            if ".pulses_new" in str(a):
                raise OSError(5, "killed while swapping")
            return real_replace(a, b, *args, **kw)

        with _FakeXDev(also_link=True), mock.patch.object(T.os, "replace", _die):
            res = T.move_in(src, dst, preserve=True)
        self.assertFalse(res.ok)
        self.assertEqual(dst.read_bytes(), old,
                         "目标端必须是完整的旧文件，不能出现半截新文件")
        self.assertEqual(list(dst.parent.glob(".*pulses_new")), [])

    def test_batch_helpers(self):
        self.assertTrue(T.is_transient_name(".a.pulses_new"))
        self.assertTrue(T.is_transient_name(".config.pulses_tmp"))
        self.assertFalse(T.is_transient_name("mods"))
        self.assertEqual(T.tmp_name(Path("/x/y.jar")).name,
                         ".y.jar.pulses_new")
        ok, _msg = T.have_space_for(1024, self.tmp)
        self.assertTrue(ok)
        bad, msg = T.have_space_for(1 << 60, self.tmp)
        self.assertFalse(bad)
        self.assertIn("空间不足", msg)

        stale = self.tmp / "inst" / ".old.pulses_new"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"x")
        self.assertEqual(T.cleanup_transient(self.tmp / "inst"), 1)
        self.assertFalse(stale.exists())

    def test_windows_long_path_prefix(self):
        saved = T._IS_WINDOWS
        try:
            T._IS_WINDOWS = True
            short = T.sys_path(Path("C:/a/b"))
            self.assertEqual(short, "C:/a/b")
            long = "C:/" + "d" * 300 + "/x.txt"
            self.assertTrue(T.sys_path(long).startswith("\\\\?\\"))
            unc = "\\\\server\\share\\" + "d" * 300
            self.assertTrue(T.sys_path(unc).startswith("\\\\?\\UNC\\"))
        finally:
            T._IS_WINDOWS = saved


# ======================================================================
# 来源层：缓存优先 / 陈旧缓存不可见 / 保留名过滤
# ======================================================================
class TestPlanSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-ps-")
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "cache"
        self.ov = self.tmp / "overrides"

    def tearDown(self):
        self._tmp.cleanup()

    def test_cache_wins_over_overrides(self):
        _write(self.cache / "mods" / "a.jar", "from-cache")
        _write(self.ov / "mods" / "a.jar", "from-overrides")
        _write(self.ov / "mods" / "b.jar", "only-overrides")
        src = PlanSource([SourceLayer(self.cache, allowed={"mods/a.jar"}),
                          SourceLayer(self.ov, consume=True)])
        self.assertEqual(src.path_for(Path("mods/a.jar")).read_text(),
                         "from-cache")
        self.assertEqual(src.path_for(Path("mods/b.jar")).read_text(),
                         "only-overrides")

    def test_stale_cache_files_are_invisible(self):
        """缓存里上一次更新剩下的文件不能混进这一次。"""
        _write(self.cache / "mods" / "stale.jar", "stale")
        src = PlanSource([SourceLayer(self.cache, allowed={"mods/fresh.jar"})])
        self.assertIsNone(src.find(Path("mods/stale.jar")))

    def test_ignore_predicate_and_walk(self):
        _write(self.ov / "modrinth.index.json", "{}")
        _write(self.ov / "options.txt", "fov:90")
        _write(self.ov / "mods" / "a.jar", "J")
        _write(self.ov / "mods" / "sub" / "b.jar", "B")
        src = PlanSource([SourceLayer(
            self.ov, consume=True,
            ignore=lambda rel: rel.as_posix() == "modrinth.index.json")])
        self.assertIsNone(src.find(Path("modrinth.index.json")))
        files, dirs = src.walk_dir(Path("mods"))
        self.assertEqual(sorted(f.as_posix() for f in files),
                         ["mods/a.jar", "mods/sub/b.jar"])
        self.assertEqual([d.as_posix() for d in dirs], ["mods/sub"])

    def test_take_marks_consume(self):
        _write(self.ov / "mods" / "a.jar", "CONTENT")
        _write(self.cache / "mods" / "b.jar", "CACHE")
        src = PlanSource([SourceLayer(self.cache, allowed={"mods/b.jar"},
                                      consume=False),
                          SourceLayer(self.ov, consume=True)])
        dst = self.tmp / "inst"
        self.assertTrue(src.take(Path("mods/a.jar"), dst / "mods/a.jar").ok)
        self.assertFalse((self.ov / "mods" / "a.jar").exists(),
                         "overrides 是临时解压产物，应当被消耗")
        self.assertTrue(src.take(Path("mods/b.jar"), dst / "mods/b.jar").ok)
        self.assertTrue((self.cache / "mods" / "b.jar").exists(),
                        "下载缓存要保留")


# ======================================================================
# 计划 + 执行 + 校验（按需取源，不再有 _merged 副本）
# ======================================================================
class _Change:
    def __init__(self, rel: str, kind, folder: bool = False):
        self.rel_path = Path(rel)
        self.kind = kind
        self.is_folder_level = folder


class _Diff:
    def __init__(self, added=(), modified=(), deleted=(),
                 folders_added=(), folders_modified=(), folders_deleted=()):
        self.added = ([_Change(r, ChangeKind.ADDED) for r in added]
                      + [_Change(r, ChangeKind.ADDED, True)
                         for r in folders_added])
        self.modified = ([_Change(r, ChangeKind.MODIFIED) for r in modified]
                         + [_Change(r, ChangeKind.MODIFIED, True)
                            for r in folders_modified])
        self.deleted = ([_Change(r, ChangeKind.DELETED) for r in deleted]
                        + [_Change(r, ChangeKind.DELETED, True)
                           for r in folders_deleted])


class TestPlanExecution(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-pe-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "instance"
        self.cache = self.tmp / "cache"
        self.ov = self.tmp / "overrides"

    def tearDown(self):
        self._tmp.cleanup()

    def _source(self, allowed=None):
        layers = []
        if self.cache.is_dir():
            layers.append(SourceLayer(self.cache, allowed=allowed,
                                      consume=False))
        layers.append(SourceLayer(self.ov, consume=True))
        return PlanSource(layers)

    def test_build_plan_records_expectations(self):
        _write(self.cache / "mods" / "new.jar", "NEWFILE")
        diff = _Diff(added=["mods/new.jar"])
        plan = build_plan(diff, {"mods": True}, {},
                          old_root=self.inst, source=self._source())
        self.assertEqual([p.as_posix() for p in plan.copy], ["mods/new.jar"])
        self.assertEqual(plan.expect_size.get("mods/new.jar"),
                         len("NEWFILE"))

    def test_execute_moves_and_keeps_cache(self):
        data = os.urandom(50_000)
        (self.cache / "mods").mkdir(parents=True)
        (self.cache / "mods" / "new.jar").write_bytes(data)
        _write(self.inst / "mods" / "old.jar", "old")
        diff = _Diff(added=["mods/new.jar"], deleted=["mods/old.jar"])
        # 删除项要完全匹配策略才会真的删
        src = self._source(allowed={"mods/new.jar"})
        plan = build_plan(diff, {"mods": True},
                          {"mods": Strategy.FULL_MATCH},
                          old_root=self.inst, source=src)
        report = execute_plan(plan, self.inst, source=src)
        self.assertEqual(report["failed"], [])
        self.assertEqual((self.inst / "mods" / "new.jar").read_bytes(), data)
        self.assertFalse((self.inst / "mods" / "old.jar").exists())
        self.assertTrue((self.cache / "mods" / "new.jar").exists(),
                        "缓存不能被吃掉")
        self.assertEqual(verify_after_update(plan, self.inst), [])

    def test_verify_needs_no_source_after_move(self):
        """源被搬走（同卷移动）后校验仍然成立——靠事先登记的期望值。"""
        data = os.urandom(20_000)
        _write(self.ov / "mods" / "x.jar", data.decode("latin-1"))
        raw = self.ov / "mods" / "x.jar"
        raw.write_bytes(data)
        src = self._source()
        plan = build_plan(_Diff(added=["mods/x.jar"]), {"mods": True}, {},
                          old_root=self.inst, source=src)
        execute_plan(plan, self.inst, source=src)
        self.assertFalse(raw.exists(), "overrides 源已被消耗")
        self.assertEqual(verify_after_update(plan, self.inst), [])
        # 目标被改坏 → 校验要能发现
        (self.inst / "mods" / "x.jar").write_bytes(b"corrupted")
        failed = verify_after_update(plan, self.inst)
        self.assertEqual([f["rel"] for f in failed], ["mods/x.jar"])

    def test_full_match_dir_replace_is_transactional_and_moves(self):
        _write(self.ov / "config" / "shipped.toml", "new")
        _write(self.inst / "config" / "user-only.toml", "mine")
        src = self._source()
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True}, {},
                          old_root=self.inst, source=src)
        # 目录级 MODIFIED 走 replace_dirs
        self.assertEqual([p.as_posix() for p, _s in plan.replace_dirs],
                         ["config"])
        report = execute_plan(plan, self.inst, source=src)
        self.assertEqual(report["failed"], [])
        self.assertTrue((self.inst / "config" / "shipped.toml").is_file())
        self.assertFalse((self.inst / "config" / "user-only.toml").exists(),
                         "完全匹配语义：整目录替换")
        self.assertEqual(verify_after_update(plan, self.inst), [])
        leftovers = [p.name for p in self.inst.iterdir() if "pulses" in p.name]
        self.assertEqual(leftovers, [])

    def test_skip_same_dir_keeps_player_file_and_verifies(self):
        """跳过重名：玩家自己的版本保留，且**不能**被误报成校验失败。"""
        _write(self.ov / "config" / "keep.toml", "PLAYER-VERSION-LONGER")
        _write(self.ov / "config" / "new.toml", "shipped")
        _write(self.inst / "config" / "keep.toml", "mine")
        src = self._source()
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True},
                          {"config": Strategy.SKIP_SAME},
                          old_root=self.inst, source=src)
        report = execute_plan(plan, self.inst, source=src)
        self.assertEqual(report["failed"], [])
        self.assertEqual((self.inst / "config" / "keep.toml").read_text(),
                         "mine")
        self.assertTrue((self.inst / "config" / "new.toml").is_file())
        self.assertEqual(verify_after_update(plan, self.inst), [],
                         "玩家保留的版本不该被报成大小不符")

    def test_replace_same_dir_overwrites_only_same_name(self):
        _write(self.ov / "config" / "same.toml", "new")
        _write(self.inst / "config" / "same.toml", "old")
        _write(self.inst / "config" / "mine.toml", "mine")
        src = self._source()
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True},
                          {"config": Strategy.REPLACE_SAME},
                          old_root=self.inst, source=src)
        execute_plan(plan, self.inst, source=src)
        self.assertEqual((self.inst / "config" / "same.toml").read_text(),
                         "new")
        self.assertTrue((self.inst / "config" / "mine.toml").is_file())

    def test_dir_replace_rolls_back_when_source_missing(self):
        _write(self.inst / "config" / "keep.toml", "keep")
        src = self._source()
        # 目录级变更但来源里什么都没有 → 目录操作必须失败并回滚
        plan = UpdatePlan(replace_dirs=[(Path("config"), Strategy.FULL_MATCH)])
        report = execute_plan(plan, self.inst, source=src)
        self.assertEqual(len(report["failed"]), 1)
        self.assertEqual((self.inst / "config" / "keep.toml").read_text(),
                         "keep")

    def test_batching_parameters_do_not_change_result(self):
        for i in range(50):
            _write(self.ov / "mods" / f"{i}.jar", f"content-{i}")
        src = self._source()
        plan = build_plan(_Diff(added=[f"mods/{i}.jar" for i in range(50)]),
                          {"mods": True}, {}, old_root=self.inst, source=src)
        report = execute_plan(plan, self.inst, source=src,
                              chunk_bytes=1024, batch_bytes=4096,
                              batch_pause_ms=0.001)
        self.assertEqual(report["failed"], [])
        for i in range(50):
            self.assertEqual(
                (self.inst / "mods" / f"{i}.jar").read_text(), f"content-{i}")

    def test_legacy_root_call_still_works(self):
        """老调用方式（给 new_root 目录）保持可用：复制语义 + 源保留。"""
        _write(self.ov / "mods" / "a.jar", "CONTENT")
        plan = UpdatePlan(copy=[Path("mods/a.jar")])
        report = execute_plan(plan, self.inst, self.ov)
        self.assertEqual(report["failed"], [])
        self.assertTrue((self.ov / "mods" / "a.jar").exists())
        self.assertEqual(verify_after_update(plan, self.inst, self.ov), [])
        self.assertEqual(verify_after_update(plan, self.inst), [])


# ======================================================================
# T1 / T4  player_view：后台线程 + 完成后复位
# ======================================================================
def _install_stubs():
    if "customtkinter" not in sys.modules:
        stubs.install()


class _SyncAfter:
    """把 after(0, fn) 变成"立刻执行"，让后台线程的收尾回调可测。"""

    def __init__(self, view):
        self.view = view

    def __enter__(self):
        self._saved = getattr(self.view, "after", None)

        def _after(_ms, fn=None, *a, **k):
            if fn is not None:
                fn(*a, **k)
            return "after#0"

        self.view.after = _after
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            self.view.after = self._saved
        return False


class TestApplyWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-aw-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        import main as main_mod
        from app.core.differ import DiffResult
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        WindowClass = type("WindowClass", (MainWindow, AppBase), {})
        self.app = WindowClass()
        self.pv = self.app.player_view
        self.tmp = Path(self._tmp.name) / f"case{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.pv.pack_info = types.SimpleNamespace(path=self.tmp / "instance",
                                                  name="t")
        (self.tmp / "instance").mkdir(parents=True, exist_ok=True)
        self.pv._overrides_root = self.tmp / "overrides"
        self.pv._overrides_root.mkdir(parents=True, exist_ok=True)
        self.pv._temp_dir = types.SimpleNamespace(name=str(self.tmp))
        self.pv._cache_root = self.tmp / "cache"
        self.pv._cache_root.mkdir(parents=True, exist_ok=True)
        self.pv.diff = DiffResult()
        self.pv._download_tasks = []
        self.pv._completed_files = []

    def test_plan_and_apply_run_off_the_ui_thread(self):
        """T1 回归：计划/搬运/校验都不许在 UI 线程上做。"""
        import app.ui.player_view as pv_mod
        calls = []
        real_build = pv_mod.build_plan
        real_exec = pv_mod.execute_plan

        def _build(*a, **k):
            calls.append(("build", threading.current_thread().name))
            return real_build(*a, **k)

        def _exec(*a, **k):
            calls.append(("exec", threading.current_thread().name))
            return real_exec(*a, **k)

        main_name = threading.current_thread().name
        _write(self.pv._overrides_root / "mods" / "a.jar", "CONTENT")
        self.pv.diff = _Diff(added=["mods/a.jar"])
        self.pv.strategy_table.set_folders(["mods"])
        self.pv.strategy_table.set_strategies(
            {"mods": Strategy.REPLACE_SAME.value})
        self.pv.strategy_table.set_checked_all(True)

        with _SyncAfter(self.pv), \
                mock.patch.object(pv_mod, "build_plan", _build), \
                mock.patch.object(pv_mod, "execute_plan", _exec):
            self.pv._start_apply()
            self.pv._download_thread = None
            for t in threading.enumerate():
                if t.name.startswith("Thread-"):
                    t.join(timeout=5)

        self.assertTrue(calls, "计划/执行没有被调用")
        for what, name in calls:
            self.assertNotEqual(name, main_name,
                                f"{what} 仍在 UI 线程上执行（会冻住界面）")
        self.assertTrue((self.pv.pack_info.path / "mods" / "a.jar").is_file())

    def test_state_reset_after_successful_apply(self):
        """T4 回归：应用完成后不能留着过期变更列表和"再点一次"。"""
        _write(self.pv._overrides_root / "mods" / "a.jar", "CONTENT")
        self.pv.diff = _Diff(added=["mods/a.jar"])
        self.pv.strategy_table.set_folders(["mods"])
        self.pv.strategy_table.set_strategies(
            {"mods": Strategy.REPLACE_SAME.value})
        self.pv.strategy_table.set_checked_all(True)

        with _SyncAfter(self.pv):
            self.pv._start_apply()
            for t in threading.enumerate():
                if t.name.startswith("Thread-"):
                    t.join(timeout=5)

        self.assertIsNone(self.pv.diff, "完成后 diff 必须清掉")
        self.assertIsNone(self.pv.plan)
        self.assertEqual(self.pv._download_tasks, [])
        self.assertEqual(self.pv._phase, "idle")
        self.assertEqual(self.pv._btn_state, "ready")
        self.assertIn("已全部应用", self.pv._placeholder_text)

    def test_no_second_apply_from_stale_plan(self):
        """复位之后直接再点一次「开始更新」不会拿旧计划重跑。"""
        self.pv.diff = None
        with mock.patch("app.ui.player_view.execute_plan") as ex:
            self.pv._start_apply()
        ex.assert_not_called()


# ======================================================================
# T2 进程级验证：应用途中被强杀，整合包里不能出现半截文件
# ======================================================================
_KILL_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.core.updater import PlanSource, SourceLayer, UpdatePlan, execute_plan

inst = Path(sys.argv[2]); src = Path(sys.argv[3])
rels = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())

real = os.replace
def _replace(a, b, *args, **kw):
    if ".pulses_new" not in str(a):
        raise OSError(18, "Invalid cross-device link")
    return real(a, b, *args, **kw)
os.replace = _replace
os.link = lambda *a, **k: (_ for _ in ()).throw(OSError(1, "no link"))

execute_plan(UpdatePlan(copy=rels), inst,
             source=PlanSource([SourceLayer(src, consume=True)]),
             chunk_bytes=64 * 1024, pace_ms=1.0)
"""


class TestKillDuringApply(unittest.TestCase):
    """真的把进程 SIGKILL 掉：目标端只允许是"完整旧文件"或"完整新文件"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-kill-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_killed_copy_leaves_no_truncated_file(self):
        import subprocess
        src = self.tmp / "cache"
        inst = self.tmp / "instance"
        n, size = 30, 3 * 1024 * 1024
        old_blob = {}
        for i in range(n):
            rel = Path("mods") / f"m{i:02d}.jar"
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(bytes([i]) * size)
            old = b"OLD" * (1000 + i)
            (inst / rel).parent.mkdir(parents=True, exist_ok=True)
            (inst / rel).write_bytes(old)
            old_blob[rel.as_posix()] = old

        script = self.tmp / "child.py"
        script.write_text(_KILL_CHILD, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script), str(_ROOT), str(inst), str(src)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等到真的开始搬了（出现临时文件）再杀，确保杀在"搬运途中"
        import time as _t
        t0 = _t.time()
        while _t.time() - t0 < 30:
            if list(inst.rglob(".*pulses_new")):
                break
            if proc.poll() is not None:
                break
            _t.sleep(0.01)
        deadline = _t.time() - t0
        alive = proc.poll() is None
        proc.kill()
        proc.wait(timeout=10)
        self.assertTrue(alive, "子进程跑完了，没能在搬运途中强杀（测试无效）")

        complete = 0
        for rel_key, old in old_blob.items():
            got = (inst / rel_key).read_bytes()
            is_old = got == old
            is_new = got == bytes([int(Path(rel_key).stem[1:])]) * size
            self.assertTrue(is_old or is_new,
                            f"{rel_key} 既不是完整旧文件也不是完整新文件"
                            f"（{len(got)} 字节，被截断了）")
            complete += 1
        self.assertEqual(complete, n)

        # 留下的只能是点号开头的临时文件，而且能被自动清掉
        stray = [p for p in inst.rglob("*")
                 if p.is_file() and not p.name.startswith(".")]
        self.assertEqual(len(stray), n, "整合包里出现了非预期的多余文件")
        removed = T.cleanup_transient(inst)
        self.assertGreaterEqual(removed, 1,
                                f"强杀应当留下临时文件（等了 {deadline:.2f}s）")
        self.assertEqual(list(inst.rglob(".*pulses_new")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

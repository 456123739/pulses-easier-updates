"""
test_recover.py — 批次2「健壮性」回归测试
------------------------------------------------
  R1  强杀留下的残渣没人管：`.xxx.pulses_tmp`（半个目录）/
      `.yyy.pulses_new`（半截临时文件）
  R2  没有磁盘空间预检：写到一半才发现 ENOSPC，失败面巨大
  R3  检查点 O(n²)：每 25 个文件重写一次全量 done 列表
      （实测 2 万文件占应用耗时 74%）
  R4  删除被移除的目录时逐文件 unlink（比 rmtree 慢约 7 倍）
  R5  收尾校验阶段没有检查点：搬运完了、校验途中被杀 = 什么都发现不了

运行：  python3 tests/test_recover.py
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                     # noqa: E402

from app.core import checkpoint as cp_mod        # noqa: E402
from app.core import recover as R                # noqa: E402
from app.core import transfer as T               # noqa: E402
from app.core import updater as U                # noqa: E402
from app.core.updater import (                   # noqa: E402
    InsufficientSpace,
    PlanSource,
    SourceLayer,
    UpdatePlan,
    build_plan,
    execute_plan,
    verify_after_update,
)
from app.config import ChangeKind, Strategy      # noqa: E402


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Change:
    def __init__(self, rel: str, kind, folder: bool = False):
        self.rel_path = Path(rel)
        self.kind = kind
        self.is_folder_level = folder


class _Diff:
    def __init__(self, added=(), modified=(), deleted=(),
                 folders_deleted=(), folders_modified=()):
        self.added = [_Change(r, ChangeKind.ADDED) for r in added]
        self.modified = ([_Change(r, ChangeKind.MODIFIED) for r in modified]
                         + [_Change(r, ChangeKind.MODIFIED, True)
                            for r in folders_modified])
        self.deleted = ([_Change(r, ChangeKind.DELETED) for r in deleted]
                        + [_Change(r, ChangeKind.DELETED, True)
                           for r in folders_deleted])


# ======================================================================
# R1  残渣自检
# ======================================================================
class TestRecoverLeftovers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-rec-")
        self.tmp = Path(self._tmp.name)
        self.pack = self.tmp / "instance"
        self.pack.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_restores_interrupted_dir_replace(self):
        """替换死在"旧目录已改名、新目录还没写出来" → 旧内容必须回来。"""
        _write(self.pack / ".config.pulses_tmp" / "shipped.toml", "old")
        self.assertFalse((self.pack / "config").exists())

        stats = R.recover_leftovers(self.pack)
        self.assertEqual(stats["restored"], ["config"])
        self.assertEqual((self.pack / "config" / "shipped.toml").read_text(),
                         "old")
        self.assertFalse((self.pack / ".config.pulses_tmp").exists())

    def test_partial_new_dir_is_replaced_by_backup(self):
        """新目录只写了一半 → 仍然回滚到完整的旧内容（宁可不丢东西）。"""
        _write(self.pack / "config" / "half.toml", "HALF")
        _write(self.pack / ".config.pulses_tmp" / "a.toml", "old-a")
        _write(self.pack / ".config.pulses_tmp" / "b.toml", "old-b")

        stats = R.recover_leftovers(self.pack)
        self.assertEqual(stats["restored"], ["config"])
        self.assertTrue((self.pack / "config" / "a.toml").is_file())
        self.assertTrue((self.pack / "config" / "b.toml").is_file())
        self.assertFalse((self.pack / "config" / "half.toml").exists())

    def test_removes_transfer_temp_files(self):
        _write(self.pack / "mods" / ".a.jar.pulses_new", "half")
        _write(self.pack / "mods" / "a.jar", "good")
        stats = R.recover_leftovers(self.pack)
        self.assertEqual(stats["removed_files"], [".a.jar.pulses_new"])
        self.assertTrue((self.pack / "mods" / "a.jar").is_file())

    def test_numbered_sidecar_and_false_positives(self):
        _write(self.pack / ".config.pulses_tmp2" / "x", "old")
        _write(self.pack / ".notareplacement.toml", "keep")
        _write(self.pack / "notes.pulses_tmpX", "keep")
        self.assertTrue(R.has_leftovers(self.pack))
        stats = R.recover_leftovers(self.pack)
        self.assertEqual(stats["restored"], ["config"])
        self.assertTrue((self.pack / ".notareplacement.toml").is_file())
        self.assertTrue((self.pack / "notes.pulses_tmpX").is_file())

    def test_fresh_leftovers_can_be_skipped(self):
        _write(self.pack / "mods" / ".a.jar.pulses_new", "half")
        stats = R.recover_leftovers(self.pack, max_age_s=3600)
        self.assertEqual(stats["removed_files"], [])
        self.assertEqual(len(stats["skipped"]), 1)
        self.assertTrue(R.has_leftovers(self.pack))

    def test_summary_text(self):
        self.assertEqual(R.summary({"restored": ["a"], "removed_files": ["b"]}),
                         "回滚 1 个没做完的目录替换，清理 1 个搬运临时文件")
        self.assertEqual(R.summary({}), "")

    def test_differ_ignores_transient_names(self):
        from app.config import is_local_only_name
        self.assertTrue(is_local_only_name(".a.jar.pulses_new"))
        self.assertTrue(is_local_only_name(".config.pulses_tmp"))
        self.assertFalse(is_local_only_name("mods"))
        self.assertFalse(is_local_only_name("a.jar"))


class TestPlayerViewWiring(unittest.TestCase):
    """比对/应用之前的"就地自检"确实接上了。"""

    @classmethod
    def setUpClass(cls):
        if "customtkinter" not in sys.modules:
            stubs.install()

    def test_recover_leftovers_is_wired_into_view(self):
        import types
        from app.ui.player_view import PlayerView
        with tempfile.TemporaryDirectory(prefix="easier-wr-") as td:
            pack = Path(td) / "instance"
            _write(pack / ".config.pulses_tmp" / "a.toml", "old")
            _write(pack / "mods" / ".b.jar.pulses_new", "half")
            logs = []
            view = PlayerView.__new__(PlayerView)
            view.pack_info = types.SimpleNamespace(path=pack, name="t")
            view.log = types.SimpleNamespace(
                log=lambda level, msg: logs.append((level, str(msg))))
            view._recover_apply_leftovers()
            self.assertTrue((pack / "config" / "a.toml").is_file())
            self.assertFalse((pack / "mods" / ".b.jar.pulses_new").exists())
            self.assertTrue(any("没有正常结束" in m for _l, m in logs),
                            f"自检结果没有写进日志：{logs}")


# ======================================================================
# R3 / R5  检查点
# ======================================================================
class TestCheckpointWriter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.core import database as db
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-cp-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        cls.dbdir = Path(cls._tmp.name) / "db"
        db.create_database(cls.dbdir)
        db.set_db_path(cls.dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        self.pack = Path(self._tmp.name) / f"pack{id(self)}"
        self.pack.mkdir(parents=True, exist_ok=True)
        cp_mod.clear_checkpoint(self.pack)

    def test_writer_does_not_rewrite_growing_json(self):
        """
        R3 回归：2 万个文件时，写盘次数必须有界，而且每次写出的内容
        不能越来越大（旧实现每 25 个文件重写一遍全量 done 列表，
        实测占应用阶段 74% 的耗时）。
        """
        sizes: list[int] = []
        real = cp_mod._write_cp

        def _spy(path, cp):
            ok = real(path, cp)
            try:
                sizes.append(path.stat().st_size)      # 真正写到盘上的大小
            except OSError:
                pass
            return ok

        with mock.patch.object(cp_mod, "_write_cp", _spy):
            w = cp_mod.CheckpointWriter(self.pack)
            for i in range(20000):
                w.done(f"mods/f{i}.jar")
            w.finish("verify")
        self.assertEqual(w.cp.done_count, 20000)
        self.assertLessEqual(len(sizes), 20, "写盘次数随文件数增长")
        self.assertLessEqual(max(sizes), 16384,
                             "每次写到盘上的 JSON 必须是有界的")
        self.assertLess(sum(sizes), 300_000,
                        "累计写盘量必须很小（旧实现是数百 MB）")
        # 时间节流：连续 done 不会每次都写
        w2 = cp_mod.CheckpointWriter(self.pack, min_interval=30.0,
                                     every=10 ** 9)
        before = len(sizes)
        with mock.patch.object(cp_mod, "_write_cp",
                               side_effect=lambda p, cp:
                               sizes.append(1) or True):
            for i in range(500):
                w2.done(f"x{i}")
        self.assertLessEqual(len(sizes) - before, 2,
                             "时间节流没有生效")

    def test_done_list_is_capped_but_count_is_exact(self):
        w = cp_mod.CheckpointWriter(self.pack, min_interval=0.0, every=10)
        for i in range(5000):
            w.done(f"f{i}")
        w.finish("verify")
        cp = cp_mod.load_checkpoint(self.pack)
        self.assertIsNotNone(cp)
        self.assertEqual(cp.done_count, 5000)
        self.assertLessEqual(len(cp.done), cp_mod._KEEP_DONE)

    def test_stage_transitions_are_written_immediately(self):
        w = cp_mod.CheckpointWriter(self.pack, min_interval=999.0)
        w.stage("copy")
        self.assertEqual(cp_mod.load_checkpoint(self.pack).stage, "copy")
        w.stage("delete", remaining=["mods/gone.jar"])
        cp = cp_mod.load_checkpoint(self.pack)
        self.assertEqual(cp.stage, "delete")
        self.assertEqual(cp.remaining, ["mods/gone.jar"])
        w.finish("verify")
        cp = cp_mod.load_checkpoint(self.pack)
        self.assertEqual(cp.stage, "verify")
        self.assertEqual(cp.remaining, [])

    def test_verify_stage_is_reported_as_interrupted(self):
        w = cp_mod.CheckpointWriter(self.pack)
        w.finish("verify")
        found = [c for c in cp_mod.scan_checkpoints()
                 if c.pack_root == str(self.pack)]
        self.assertEqual(len(found), 1, "停在 verify 必须被当成中断")
        cp_mod.mark_applied(self.pack)
        found = [c for c in cp_mod.scan_checkpoints()
                 if c.pack_root == str(self.pack)]
        self.assertEqual(found, [])
        self.assertIsNone(cp_mod.load_checkpoint(self.pack))

    def test_checkpoint_json_is_compact(self):
        w = cp_mod.CheckpointWriter(self.pack, min_interval=0.0)
        for i in range(50):
            w.done(f"mods/f{i}.jar")
        w.save(force=True)
        d = cp_mod._checkpoint_dir()
        raw = (d / f"{cp_mod._pack_hash(self.pack)}.json").read_text("utf-8")
        self.assertNotIn("\n  ", raw, "检查点不该再带缩进")
        self.assertLess(len(raw), 4000)


# ======================================================================
# R2  磁盘空间预检
# ======================================================================
class TestSpaceCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-sp-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "instance"
        self.src = self.tmp / "src"
        self.inst.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            _write(self.src / "mods" / f"{i}.jar", "x" * 100)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self):
        src = PlanSource([SourceLayer(self.src, consume=True)])
        diff = _Diff(added=[f"mods/{i}.jar" for i in range(4)])
        plan = build_plan(diff, {"mods": True}, {},
                          old_root=self.inst, source=src)
        return plan, src

    def test_same_volume_needs_no_extra_space(self):
        plan, src = self._plan()
        self.assertEqual(U.estimate_copy_bytes(plan, self.inst, src), 0)

    def test_cross_volume_counts_bytes(self):
        import os as _os
        plan, src = self._plan()
        real_stat = _os.stat

        def _fake_stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            # 只把"源目录那侧"的设备号换掉，其它字段保持真实（is_file 要用）
            dev = 999 if str(self.src) in str(path) else st.st_dev
            return _os.stat_result((
                st.st_mode, st.st_ino, dev, st.st_nlink, st.st_uid,
                st.st_gid, st.st_size, st.st_atime, st.st_mtime,
                st.st_ctime))

        with mock.patch.object(U.os, "stat", _fake_stat):
            self.assertEqual(
                U.estimate_copy_bytes(plan, self.inst, src), 400)

    def test_refuses_before_touching_anything(self):
        plan, src = self._plan()
        with mock.patch.object(T, "have_space_for",
                               return_value=(False, "磁盘空间不足：需要约 1GB")):
            with self.assertRaises(InsufficientSpace) as ctx:
                execute_plan(plan, self.inst, source=src)
        self.assertIn("磁盘空间不足", str(ctx.exception))
        self.assertFalse((self.inst / "mods" / "0.jar").exists(),
                         "拒绝之后一个字节都不该写")
        self.assertTrue((self.src / "mods" / "0.jar").is_file(),
                        "源也不能被动过")

    def test_can_be_disabled(self):
        plan, src = self._plan()
        with mock.patch.object(T, "have_space_for",
                               return_value=(False, "no space")):
            report = execute_plan(plan, self.inst, source=src,
                                  space_check=False)
        self.assertEqual(report["failed"], [])
        self.assertTrue((self.inst / "mods" / "0.jar").is_file())


# ======================================================================
# R4  删除目录
# ======================================================================
class TestDirDelete(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-dd-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "instance"
        self.src = self.tmp / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        for i in range(200):
            _write(self.inst / "oldpack" / f"f{i}.bin", "x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_deleted_folder_is_one_rmtree(self):
        src = PlanSource([SourceLayer(self.src, consume=True)])
        plan = build_plan(_Diff(folders_deleted=["oldpack"]), {"oldpack": True},
                          {"oldpack": Strategy.FULL_MATCH},
                          old_root=self.inst, source=src)
        self.assertEqual([p.as_posix() for p in plan.delete], ["oldpack"],
                         "整目录删除不该被展开成 200 个文件条目")
        with mock.patch.object(U.shutil, "rmtree",
                               wraps=U.shutil.rmtree) as spy:
            report = execute_plan(plan, self.inst, source=src,
                                  space_check=False)
        self.assertEqual(report["deleted"], 1)
        self.assertEqual(spy.call_count, 1)
        self.assertFalse((self.inst / "oldpack").exists())
        self.assertEqual(verify_after_update(plan, self.inst), [])

    def test_skip_strategy_keeps_folder(self):
        src = PlanSource([SourceLayer(self.src, consume=True)])
        plan = build_plan(_Diff(folders_deleted=["oldpack"]), {"oldpack": True},
                          {"oldpack": Strategy.REPLACE_SAME},
                          old_root=self.inst, source=src)
        self.assertEqual(plan.delete, [])
        self.assertEqual([p.as_posix() for p in plan.skip], ["oldpack"])


# ======================================================================
# 批次3：内容级校验 / 缓存回收 / 进度加权
# ======================================================================
class TestBatch3(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-b3-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "instance"
        self.src = self.tmp / "src"
        self.inst.mkdir(parents=True)
        data = b"REAL-CONTENT" * 100
        _write(self.src / "mods" / "a.jar", data.decode("latin-1"))
        (self.src / "mods" / "a.jar").write_bytes(data)
        import hashlib
        self.sha = hashlib.sha1(data).hexdigest()
        self.size = len(data)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, content: bytes):
        dst = self.inst / "mods" / "a.jar"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)
        plan = UpdatePlan(copy=[Path("mods/a.jar")])
        plan.expect_size["mods/a.jar"] = self.size
        plan.expect_hash["mods/a.jar"] = f"sha1:{self.sha}"
        return plan

    def test_deep_verify_catches_same_size_corruption(self):
        """大小一样、内容不对：只有内容级校验能发现。"""
        plan = self._plan(b"X" * self.size)
        self.assertEqual(verify_after_update(plan, self.inst), [],
                         "只比大小时发现不了")
        failed = verify_after_update(plan, self.inst, deep=True)
        self.assertEqual([f["rel"] for f in failed], ["mods/a.jar"])
        self.assertIn("内容校验失败", failed[0]["error"])

    def test_deep_verify_passes_on_good_file_and_reports_stats(self):
        plan = self._plan(b"REAL-CONTENT" * 100)
        stats: dict = {}
        self.assertEqual(
            verify_after_update(plan, self.inst, deep=True, stats=stats), [])
        self.assertEqual(stats["deep_files"], 1)
        self.assertEqual(stats["deep_bytes"], self.size)

    def test_release_pack_cache(self):
        from app.core import cache as cache_mod
        root = self.tmp / "cache" / "pack-fp"
        _write(root / "mods" / "a.jar", "x" * 1000)
        _write(root / "resume.json", "{}")
        ok, freed = cache_mod.release_pack_cache(root)
        self.assertTrue(ok)
        self.assertGreaterEqual(freed, 1000)
        self.assertFalse(root.exists())
        # 不存在时也不报错
        self.assertEqual(cache_mod.release_pack_cache(root), (True, 0))

    def test_progress_is_weighted_by_file_count(self):
        for i in range(5):
            _write(self.src / "config" / f"c{i}.toml", "x")
        src = PlanSource([SourceLayer(self.src, consume=True)])
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True}, {},
                          old_root=self.inst, source=src)
        seen: list[tuple[int, int]] = []
        execute_plan(plan, self.inst, source=src, space_check=False,
                     log=lambda *a: None,
                     progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        done, total = seen[-1]
        self.assertEqual(done, total)
        self.assertGreaterEqual(total, 5,
                                "整目录替换要按文件数计入进度")


if __name__ == "__main__":
    unittest.main(verbosity=2)

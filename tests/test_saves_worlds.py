"""存档级比对：saves 下每个世界一条文件夹级变更（不细到文件）。

要证明：
1. 文件系统比对 / 清单比对都按世界出 Change，且**不重复**；
2. 本地多出来的世界永远不判删除（取消导出某世界不误删其他/本地世界）；
3. 策略可以按 `saves/世界` 单独设置，并落到 build_plan；
4. 旧版 ZIP 的哈希只写顶层 `saves`（0.12 能读），逐世界哈希放独立字段。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import ChangeKind
from app.core import differ
from app.core import hashing
from app.core.updater import PlanSource, SourceLayer, build_plan, execute_plan


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class SavesWorldDiffCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-saves-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = self.tmp / "old"
        self.new = self.tmp / "new"
        _write(self.old / "saves" / "WorldA" / "level.dat", "old-A")
        _write(self.new / "saves" / "WorldA" / "level.dat", "new-A")
        _write(self.old / "saves" / "WorldB" / "level.dat", "same")
        _write(self.new / "saves" / "WorldB" / "level.dat", "same")
        _write(self.new / "saves" / "WorldNew" / "level.dat", "new")
        _write(self.old / "saves" / "WorldGone" / "level.dat", "old")
        _write(self.old / "saves" / "Personal" / "level.dat", "mine")

    def _all(self, diff):
        return diff.added + diff.modified + diff.deleted

    def test_filesystem_diff_is_per_world_and_no_delete(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        got = {(c.rel_path.as_posix(), c.kind.value, c.is_folder_level)
               for c in self._all(diff)}
        self.assertIn(("saves/WorldA", ChangeKind.MODIFIED.value, True), got)
        self.assertIn(("saves/WorldNew", ChangeKind.ADDED.value, True), got)
        # 新包没有的 WorldGone / 本地 Personal 都保留
        self.assertFalse(any(n.startswith(("saves/WorldGone", "saves/Personal"))
                             for n, _k, _f in got))
        self.assertFalse(diff.deleted)
        self.assertFalse(any(n.startswith("saves/WorldB") for n, _k, _f in got))
        self.assertTrue(all(f for n, _k, f in got if n.startswith("saves/")))
        # 同一路径同一类型只能出现一次
        keys = [(c.rel_path.as_posix(), c.kind.value) for c in self._all(diff)]
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_delete_when_pack_has_no_saves(self):
        shutil.rmtree(self.new / "saves")
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        self.assertFalse(diff.deleted)
        self.assertFalse(any(c.rel_path.parts[0] == "saves"
                             for c in diff.deleted))

    def test_manifest_diff_uses_saves_worlds_field(self):
        manifest = hashing.build_hash_manifest(self.new, whitelist=[],
                                               saves_folder_hash=False)
        self.assertIn("WorldA", manifest["saves_worlds"])
        self.assertNotIn("saves", manifest["folders"])
        self.assertNotIn("saves/WorldA", manifest["folders"])
        diff = differ.diff_from_manifest(
            self.old, manifest, index_hashes={},
            folder_hashes=manifest["folders"],
            saves_worlds=manifest["saves_worlds"],
            whitelist=[], compare_root=self.new, pack_tops={"saves"},
            threads=1)
        got = {(c.rel_path.as_posix(), c.kind.value) for c in self._all(diff)}
        self.assertIn(("saves/WorldA", ChangeKind.MODIFIED.value), got)
        self.assertIn(("saves/WorldNew", ChangeKind.ADDED.value), got)
        self.assertFalse(any(n.startswith("saves/WorldGone") for n, _k in got))
        self.assertFalse(diff.deleted)

    def test_no_local_saves_adds_each_world_once(self):
        # 黑盒回归：玩家没有 saves/ 时，清单比对不能把同一个世界 + 两次。
        manifest = hashing.build_hash_manifest(self.new, whitelist=[],
                                               saves_folder_hash=True)
        empty = self.tmp / "empty"
        empty.mkdir()
        diff = differ.diff_from_manifest(
            empty, manifest, index_hashes={},
            folder_hashes=manifest["folders"],
            saves_worlds=manifest["saves_worlds"],
            whitelist=[], compare_root=self.new, pack_tops={"saves"},
            threads=1)
        added = [c.rel_path.as_posix() for c in diff.added]
        self.assertEqual(sorted(added), ["saves/WorldA", "saves/WorldB",
                                         "saves/WorldNew"])
        self.assertEqual(len(added), len(set(added)))

    def test_index_full_path_does_not_become_world_name(self):
        # index_by_top 的 key 是完整路径；世界名必须是 TestWorld，不是
        # TestWorld/region/r.0.0.mca。
        manifest = {"files": {}, "folders": {}, "saves_worlds": {},
                    "whitelist": []}
        diff = differ.diff_from_manifest(
            self.old, manifest, index_hashes={
                "saves/TestWorld/region/r.0.0.mca": [{"sha1": "0" * 40}]},
            folder_hashes={}, saves_worlds={}, whitelist=[],
            compare_root=self.new, pack_tops={"saves"}, threads=1)
        added = [c.rel_path.as_posix() for c in diff.added
                 if c.rel_path.as_posix().startswith("saves/")]
        self.assertIn("saves/TestWorld", added)
        self.assertFalse(any(n.count("/") > 1 for n in added), added)

    def test_duplicate_tops_do_not_duplicate_changes(self):
        # 旧清单里同时有 "saves" 和 "saves/TestWorld"（两级键）时，
        # 顶层只应处理一次，玩家端不能出现两条 TestWorld。
        whole = differ.folder_hash(self.new / "saves", content=True)
        manifest = {"files": {}, "folders": {"saves": whole},
                    "saves_worlds": {}, "whitelist": []}
        diff = differ.diff_from_manifest(
            self.old, manifest, index_hashes={},
            folder_hashes={"saves": whole, "saves/TestWorld": whole},
            saves_worlds={}, whitelist=[], compare_root=self.new,
            pack_tops={"saves"}, threads=1)
        rels = [c.rel_path.as_posix() for c in self._all(diff)]
        self.assertEqual(len(rels), len(set(rels)))
        self.assertFalse(any(n.startswith("saves/TestWorld") for n in rels))

    def test_zip_manifest_has_old_client_aggregate_only(self):
        manifest = hashing.build_hash_manifest(self.new, whitelist=[],
                                               saves_folder_hash=True)
        self.assertIn("saves", manifest["folders"])
        # 0.12 会把 folders 的 key 当顶层目录：不能出现 saves/World
        self.assertFalse([k for k in manifest["folders"]
                          if k.startswith("saves/")])
        self.assertIn("WorldA", manifest["saves_worlds"])

    def test_old_manifest_falls_back_when_no_content_to_compare(self):
        whole = differ.folder_hash(self.new / "saves", content=True)
        diff = differ.diff_from_manifest(
            self.old, {"files": {}, "folders": {"saves": whole}, "whitelist": []},
            index_hashes={}, folder_hashes={"saves": whole}, whitelist=[],
            compare_root=None, pack_tops={"saves"}, threads=1)
        self.assertIn("saves", {c.rel_path.as_posix() for c in diff.modified})

    def test_plan_uses_per_world_strategy_without_delete(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        plan = build_plan(diff, {"saves": True},
                          {"saves": "完全匹配", "saves/WorldA": "跳过重名"})
        rels = {r.as_posix() for r, _s in plan.replace_dirs}
        self.assertIn("saves/WorldA", rels)
        # 本地多出来的世界不删除
        self.assertNotIn(Path("saves/WorldGone"), plan.delete)
        self.assertNotIn(Path("saves/Personal"), plan.delete)

    def test_plan_full_match_never_deletes_local_worlds(self):
        shutil.rmtree(self.new / "saves" / "WorldA")
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        plan = build_plan(diff, {"saves": True},
                          {"saves": "跳过重名", "saves/WorldA": "完全匹配"})
        self.assertFalse(any(str(p).startswith("saves/") for p in plan.delete))

    def test_execute_applies_per_world_and_keeps_local(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        src = PlanSource([SourceLayer(self.new, consume=False)])
        plan = build_plan(diff, {"saves": True},
                          {"saves": "完全匹配"}, old_root=self.old, source=src)
        report = execute_plan(plan, self.old, source=src)
        self.assertEqual(report["failed"], [])
        self.assertEqual((self.old / "saves/WorldA/level.dat").read_text(), "new-A")
        self.assertTrue((self.old / "saves/WorldNew/level.dat").is_file())
        self.assertTrue((self.old / "saves/WorldGone/level.dat").is_file())
        self.assertTrue((self.old / "saves/Personal/level.dat").is_file())
        self.assertEqual((self.old / "saves/WorldB/level.dat").read_text(), "same")


if __name__ == "__main__":
    unittest.main()

"""存档级比对：saves 下每个世界一条文件夹级变更（不细到文件）。

要证明：
1. 文件系统比对 / 清单比对都按世界出 Change；
2. 本地多出来的世界，只有"新包确实管存档"时才判删除；
3. 策略可以按 `saves/世界` 单独设置，并落到 build_plan；
4. 老包（只记了整目录 saves 哈希）自动退回原粒度，不误判。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import ChangeKind, Strategy
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
        # 两侧都有的世界
        _write(self.old / "saves" / "WorldA" / "level.dat", "old-A")
        _write(self.new / "saves" / "WorldA" / "level.dat", "new-A")
        _write(self.old / "saves" / "WorldB" / "level.dat", "same")
        _write(self.new / "saves" / "WorldB" / "level.dat", "same")
        # 只在新包 / 只在本地
        _write(self.new / "saves" / "WorldNew" / "level.dat", "new")
        _write(self.old / "saves" / "WorldGone" / "level.dat", "old")

    def _names(self, diff):
        return {(c.rel_path.as_posix(), c.kind.value, c.is_folder_level)
                for c in diff.added + diff.modified + diff.deleted}

    def test_filesystem_diff_is_per_world(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        got = self._names(diff)
        self.assertIn(("saves/WorldA", ChangeKind.MODIFIED.value, True), got)
        self.assertIn(("saves/WorldNew", ChangeKind.ADDED.value, True), got)
        self.assertIn(("saves/WorldGone", ChangeKind.DELETED.value, True), got)
        # 内容一样的 WorldB 不该出现
        self.assertFalse(any(n.startswith("saves/WorldB") for n, _k, _f in got))
        # 只能是文件夹级，不能细到文件
        self.assertTrue(all(f for n, _k, f in got if n.startswith("saves/")))

    def test_no_delete_when_pack_has_no_saves(self):
        # 新包里完全没有 saves/：本地存档不能被判成"删除"
        shutil.rmtree(self.new / "saves")
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        self.assertFalse(any(c.rel_path.parts[0] == "saves"
                             for c in diff.deleted))

    def test_manifest_diff_uses_per_world_hashes(self):
        manifest = hashing.build_hash_manifest(self.new, whitelist=[])
        self.assertIn("saves/WorldA", manifest["folders"])
        diff = differ.diff_from_manifest(
            self.old, manifest, index_hashes={}, folder_hashes=manifest["folders"],
            whitelist=[], compare_root=self.new, pack_tops={"saves"},
            threads=1)
        got = self._names(diff)
        self.assertIn(("saves/WorldA", ChangeKind.MODIFIED.value, True), got)
        self.assertIn(("saves/WorldNew", ChangeKind.ADDED.value, True), got)
        self.assertIn(("saves/WorldGone", ChangeKind.DELETED.value, True), got)

    def test_old_manifest_falls_back_when_no_content_to_compare(self):
        # 老包只有 "saves" 整目录哈希，且没有解压内容可比（index-only）：
        # 退回整目录粒度，不能凭空按世界判
        whole = differ.folder_hash(self.new / "saves", content=True)
        diff = differ.diff_from_manifest(
            self.old, {"files": {}, "folders": {"saves": whole}, "whitelist": []},
            index_hashes={}, folder_hashes={"saves": whole}, whitelist=[],
            compare_root=None, pack_tops={"saves"}, threads=1)
        self.assertIn("saves", {c.rel_path.as_posix() for c in diff.modified})

    def test_plan_uses_per_world_strategy(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        plan = build_plan(diff, {"saves": True},
                          {"saves": "完全匹配", "saves/WorldA": "跳过重名"})
        rels = {r.as_posix() for r, _s in plan.replace_dirs}
        self.assertIn("saves/WorldA", rels)
        # 删除仍然只受顶层「完全匹配」控制
        self.assertIn(Path("saves/WorldGone"), plan.delete)

    def test_plan_respects_per_world_full_match_for_delete(self):
        shutil.rmtree(self.new / "saves" / "WorldA")
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        plan = build_plan(diff, {"saves": True},
                          {"saves": "跳过重名", "saves/WorldA": "完全匹配"})
        self.assertIn(Path("saves/WorldA"), plan.delete)

    def test_execute_applies_per_world(self):
        diff = differ.diff_packs_parallel(self.old, self.new, whitelist=[],
                                          threads=1)
        src = PlanSource([SourceLayer(self.new, consume=False)])
        plan = build_plan(diff, {"saves": True},
                          {"saves": "完全匹配"}, old_root=self.old, source=src)
        report = execute_plan(plan, self.old, source=src)
        self.assertEqual(report["failed"], [])
        self.assertEqual((self.old / "saves/WorldA/level.dat").read_text(), "new-A")
        self.assertTrue((self.old / "saves/WorldNew/level.dat").is_file())
        self.assertFalse((self.old / "saves/WorldGone").exists())
        self.assertEqual((self.old / "saves/WorldB/level.dat").read_text(), "same")


if __name__ == "__main__":
    unittest.main()

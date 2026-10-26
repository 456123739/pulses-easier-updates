"""
test_scan_fast.py — 清单级比对（不解压就比对）与"无需更新"折叠

覆盖 v0.9.0 的两项改动：
  1) diff_from_manifest 与"解压后逐文件比"结果一致（这是快路径的前提）
  2) 策略表的「无需更新」折叠：没有差异的条目不再占着主列表
"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import stubs  # noqa: E402

stubs.install()          # 让 app.ui.* 能在没有真 Tk 的环境里导入

from app.core import eapack as eapack_mod                      # noqa: E402
from app.core.differ import (diff_from_manifest,               # noqa: E402
                             diff_packs_parallel)
from app.core.mrpack import locate_content_root, merge_files, parse_mrpack  # noqa: E402


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _build_pack(tmp: Path, *, with_hashes: bool = True) -> Path:
    src = tmp / "pack_src"
    _write(src / "modrinth.index.json",
           json.dumps({"formatVersion": 1, "game": "minecraft",
                       "versionId": "3.0.0", "name": "Fast",
                       "files": [], "dependencies": {}}, ensure_ascii=False))
    _write(src / "overrides" / "mods" / "keep.jar", "NEW-KEEP")
    _write(src / "overrides" / "mods" / "changed.jar", "NEW-CHANGED")
    _write(src / "overrides" / "config" / "a.toml", 'v = "new"\n')
    _write(src / "overrides" / "config" / "nested" / "b.toml", 'n = 2\n')
    _write(src / "overrides" / "资源包" / "readme.txt", "中文")
    _write(src / "overrides" / "options.txt", "fov:80\n")

    zip_path = tmp / "Fast-3.0.0.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    pack, err = parse_mrpack(zip_path)
    assert pack is not None, err

    out = tmp / "Fast-3.0.0.eapack"
    from unittest import mock
    from app.core import database as db
    export_opts = {"include_hashes": 1 if with_hashes else 0}
    with mock.patch.object(db, "get_export_options", return_value=export_opts):
        eapack_mod.export_eapack(
            source_dir=src, source_zip=zip_path, out_path=out, pack=pack,
            merged_files=merge_files(pack), strategies={"mods": "完全匹配"},
            changelog_md="# Fast\n")
    return out


def _build_local(root: Path):
    _write(root / "mods" / "keep.jar", "NEW-KEEP")        # 相同
    _write(root / "mods" / "changed.jar", "OLD")          # 内容不同
    _write(root / "mods" / "player-only.jar", "MINE")     # 只在本机
    _write(root / "config" / "a.toml", 'v = "old"\n')
    _write(root / "config" / "player.toml", "p = 1\n")
    _write(root / "options.txt", "fov:90\n")
    _write(root / "saves" / "world.dat", "WORLD")         # 包没带 → 保留


class TestManifestDiff(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-fast-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "versions" / "Fast"
        _build_local(self.inst)
        self.eapack = _build_pack(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _prepare(self):
        recorded = eapack_mod.read_hashes(self.eapack) or {}
        extract = self.tmp / "ex"
        with zipfile.ZipFile(self.eapack) as zf:
            zf.extractall(extract)
        root, _fb = locate_content_root(extract)
        whitelist = eapack_mod.read_whitelist(self.eapack) or ["mods"]
        return recorded, root, whitelist

    def _tops(self, recorded):
        return (set(recorded.get("folders") or {})
                | {p.split("/")[0] for p in (recorded.get("files") or {})})

    def test_same_result_as_filesystem_diff(self):
        recorded, root, whitelist = self._prepare()
        fs = diff_packs_parallel(self.inst, root, whitelist=whitelist)
        fast = diff_from_manifest(
            self.inst, recorded.get("files") or {},
            folder_hashes=recorded.get("folders") or {},
            whitelist=whitelist, compare_root=root,
            pack_tops=self._tops(recorded))

        def key(d):
            return (
                sorted((str(c.rel_path), c.kind, c.is_folder_level)
                       for c in d.added),
                sorted((str(c.rel_path), c.kind, c.is_folder_level)
                       for c in d.modified),
                sorted((str(c.rel_path), c.kind, c.is_folder_level)
                       for c in d.deleted),
            )

        self.assertIsNotNone(fast, "清单完整时必须能判")
        self.assertEqual(key(fs), key(fast),
                         "清单比对与文件系统比对结果必须一致")

    def test_returns_none_when_folder_hash_missing(self):
        recorded, root, whitelist = self._prepare()
        fast = diff_from_manifest(
            self.inst, recorded.get("files") or {},
            folder_hashes={},            # 非白名单目录没有记录 → 判不了
            whitelist=whitelist, compare_root=root,
            pack_tops={"config", "资源包", "mods"})
        self.assertIsNone(fast, "信息不足时必须返回 None 让调用方退回老路径")

    def test_manifest_diff_does_not_need_extracted_files(self):
        """只有根目录文件被解压时，清单比对也要能给出完整结果。"""
        recorded, root, whitelist = self._prepare()
        fast = diff_from_manifest(
            self.inst, recorded.get("files") or {},
            folder_hashes=recorded.get("folders") or {},
            whitelist=whitelist, compare_root=root,
            pack_tops=self._tops(recorded))
        self.assertIsNotNone(fast)
        modified = {str(c.rel_path) for c in fast.modified}
        self.assertIn("config", modified)      # 目录级（非白名单）
        # options.txt 永不随包（NEVER_PACK_FILES）→ 不该出现在差异里
        self.assertNotIn("options.txt", modified)

    def test_missing_manifest_falls_back(self):
        """没有清单（老包）时，调用方必须能拿到 None 信号走老路径。"""
        fast = diff_from_manifest(
            self.inst, {}, folder_hashes={}, whitelist=["mods"],
            compare_root=None, pack_tops={"config", "资源包", "mods"})
        self.assertIsNone(fast)


class TestChangeCountsFolding(unittest.TestCase):
    """策略表：扫描后没有差异的行折叠进「无需更新」。"""

    def _table(self):
        from app.ui.widgets.strategy_table import StrategyTable
        return StrategyTable(None)

    def test_unchanged_rows_are_folded(self):
        t = self._table()
        t.set_folders(["mods", "config", "saves"])
        t.set_change_counts({"mods": 2, "config": 0, "saves": 0})
        state = t._layout_state
        self.assertEqual(state["enabled"], ["mods"])
        self.assertEqual(sorted(state["unchanged"]), ["config", "saves"])
        self.assertIsNotNone(t._unchanged_header,
                             "折叠后应该有一个「无需更新」标题")

    def test_counts_clear_restores_all_rows(self):
        t = self._table()
        t.set_folders(["mods", "config"])
        t.set_change_counts({"mods": 1, "config": 0})
        t.set_change_counts({})          # 新一次扫描前清空
        self.assertEqual(sorted(t._layout_state["enabled"]),
                         ["config", "mods"],
                         "清空计数后所有行应重新显示")
        self.assertEqual(t._layout_state["unchanged"], [])


if __name__ == "__main__":
    unittest.main()

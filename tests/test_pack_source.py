"""pack_source.classify —— 开发者端 STEP 1 的"这是什么"判断。

重点不只是判对，还要证明它**便宜**：只读中央目录/目录项，不读成员内容。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core import pack_source


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _zip(path: Path, names: list[str], members: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, (members or {}).get(name, ""))
    return path


class ClassifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-src-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pack = self.tmp / "versions" / "DemoPack"
        (self.pack / "mods").mkdir(parents=True)
        (self.pack / "config").mkdir()
        _write(self.pack / "mods" / "sodium.jar", "J")

    # ---- 目录 ----
    def test_instance_dir(self):
        ref = pack_source.classify(self.pack)
        self.assertEqual(ref.kind, pack_source.KIND_INSTANCE)
        self.assertEqual(Path(ref.root), self.pack.resolve())
        self.assertEqual(ref.label, "DemoPack")

    def test_nested_dir_walks_up(self):
        ref = pack_source.classify(self.pack / "mods")
        self.assertEqual(ref.kind, pack_source.KIND_INSTANCE)
        self.assertEqual(Path(ref.root), self.pack.resolve())

    def test_any_file_inside_pack_walks_up(self):
        """和侧边栏一样：选整合包里的任意文件也算定位到整合包。"""
        ref = pack_source.classify(self.pack / "mods" / "sodium.jar")
        self.assertEqual(ref.kind, pack_source.KIND_INSTANCE)
        self.assertEqual(Path(ref.root), self.pack.resolve())

    def test_dir_without_mods_config(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(bare)
        self.assertEqual(ctx.exception.code, "NOT_A_PACK")
        self.assertIn("mods", ctx.exception.message)

    # ---- zip ----
    def test_zip_with_index(self):
        path = _zip(self.tmp / "ok.zip", ["modrinth.index.json", "overrides/mods/a.jar"])
        ref = pack_source.classify(path)
        self.assertEqual(ref.kind, pack_source.KIND_ZIP)
        self.assertEqual(ref.label, "ok")
        self.assertEqual(ref.entries, 2)

    def test_mrpack_suffix_and_nested_index(self):
        path = _zip(self.tmp / "pack.mrpack",
                    ["DemoPack/modrinth.index.json", "DemoPack/overrides/config/a.toml"])
        ref = pack_source.classify(path)
        self.assertEqual(ref.kind, pack_source.KIND_ZIP)

    def test_zip_without_index_is_rejected(self):
        path = _zip(self.tmp / "curseforge.zip", ["manifest.json", "overrides/mods/a.jar"])
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(path)
        self.assertEqual(ctx.exception.code, "NOT_A_PACK")
        self.assertIn("modrinth.index.json", ctx.exception.message)

    def test_broken_zip(self):
        path = self.tmp / "broken.zip"
        path.write_bytes(b"not a zip at all")
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(path)
        self.assertEqual(ctx.exception.code, "BAD_ZIP")

    # ---- 明确的"拖错了" ----
    def test_update_pack_is_rejected_with_reason(self):
        path = self.tmp / "DemoPack_update.eapack"
        path.write_bytes(b"PK\x03\x04")
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(path)
        self.assertEqual(ctx.exception.code, "IS_UPDATE_PACK")
        self.assertIn("更新包", ctx.exception.message)

    def test_unknown_suffix(self):
        path = self.tmp / "notes.txt"
        path.write_text("hi", encoding="utf-8")
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(path)
        self.assertEqual(ctx.exception.code, "NOT_SUPPORTED")

    def test_missing_path(self):
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify(self.tmp / "nope")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_empty_input(self):
        with self.assertRaises(pack_source.SourceError) as ctx:
            pack_source.classify("   ")
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    # ---- "便宜"是硬要求 ----
    def test_never_reads_zip_members(self):
        """判断阶段绝不能解压：成员读取一律炸掉，classify 仍要成功。"""
        path = _zip(self.tmp / "big.zip",
                    ["modrinth.index.json"] + [f"overrides/mods/m{i}.jar" for i in range(3000)])
        boom = mock.Mock(side_effect=AssertionError("classify 不该读包内成员"))
        with mock.patch.object(zipfile.ZipFile, "read", boom), \
                mock.patch.object(zipfile.ZipFile, "open", boom), \
                mock.patch.object(zipfile.ZipFile, "extract", boom), \
                mock.patch.object(zipfile.ZipFile, "extractall", boom):
            ref = pack_source.classify(path)
        self.assertEqual(ref.kind, pack_source.KIND_ZIP)
        self.assertEqual(ref.entries, 3001)
        self.assertFalse(boom.called)

    def test_too_many_entries_is_rejected_cheaply(self):
        path = _zip(self.tmp / "huge.zip",
                    ["modrinth.index.json"] + [f"f{i}" for i in range(50)])
        with mock.patch.object(pack_source, "MAX_ENTRIES", 10):
            with self.assertRaises(pack_source.SourceError) as ctx:
                pack_source.classify(path)
        self.assertEqual(ctx.exception.code, "TOO_MANY_ENTRIES")


class IndexFixtureCase(unittest.TestCase):
    """真 index 的最小合法形状（保证 classify 与 parse_mrpack 判据一致）。"""

    def test_real_index_json_is_recognised(self):
        tmp = Path(tempfile.mkdtemp(prefix="pe-src2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        index = {
            "formatVersion": 1, "game": "minecraft", "name": "Demo",
            "versionId": "1.0.0", "files": [],
            "dependencies": {"minecraft": "1.20.1"},
        }
        path = tmp / "real.mrpack"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("modrinth.index.json", json.dumps(index))
        ref = pack_source.classify(path)
        self.assertEqual(ref.kind, pack_source.KIND_ZIP)
        self.assertEqual(ref.note, "modrinth")


if __name__ == "__main__":
    unittest.main()

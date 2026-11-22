"""超级压缩（beta）与旧版互通。

口径（0.13.0 定稿）：
* 只有 super_compress=True 才写 EAPK2 + 存档去外壳；默认关 = 旧版 ZIP。
* 旧版 ZIP 用 v2 内容签名（与 v0.12.0 导出一致，含目录 marker）；
  读旧 v1 包继续兼容，新导出不再写 v1。
* 新客户端统一用 open_container 读旧 ZIP / 新容器；不依赖开关。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.core import eapack as eapack_mod
from app.core import export_rules
from app.core import instance_export
from app.core import mca
from app.core.container import open_container
from app.core.packwriter import PackWriter, resolve_export_options
from app.core.packsign import (compute_signature_v2, compute_zip_signature_v1,
                               content_entries, zip_content_entries_v2)


def _write(p: Path, data: bytes | str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)


def make_mca(chunks: list[tuple[int, bytes]] | None = None) -> bytes:
    """造一个结构合法的 region 文件；chunks = [(压缩类型, 裸字节)]。"""
    if chunks is None:
        chunks = [(2, f"chunk-{i}-".encode() + b"abc" * 200) for i in range(4)]
    header = bytearray(8192)
    body = bytearray()
    for i, (ctype, raw) in enumerate(chunks):
        if ctype == 1:
            import gzip
            payload = gzip.compress(raw)
        elif ctype == 2:
            payload = zlib.compress(raw, 6)
        else:
            payload = raw
        rec = struct.pack(">I", 1 + len(payload)) + bytes([ctype]) + payload
        rec += b"\x00" * (4096 - len(rec))
        start = 8192 + len(body)
        header[i * 4:i * 4 + 3] = (start // 4096).to_bytes(3, "big")
        header[i * 4 + 3] = 1
        body += rec
    return bytes(header) + bytes(body)


class ResolveOptionsCase(unittest.TestCase):
    def test_super_is_only_switch(self):
        got = resolve_export_options({"super_compress": True,
                                      "compact_container": False,
                                      "world_transform": False})
        self.assertTrue(got["use_v2"])
        self.assertTrue(got["mca"])
        self.assertEqual(got["level"], 17)

    def test_off_is_legacy_zip_even_if_old_db_keys_true(self):
        got = resolve_export_options({"super_compress": False,
                                      "compact_container": True,
                                      "world_transform": True})
        self.assertFalse(got["use_v2"])
        self.assertFalse(got["mca"])

    def test_level_private_override(self):
        self.assertEqual(
            resolve_export_options({"super_compress": True,
                                    "zstd_level": 19})["level"], 19)
        self.assertEqual(
            resolve_export_options({"super_compress": True,
                                    "zstd_level": 99})["level"], 22)


class ZipSignatureLayoutCase(unittest.TestCase):
    """目录 marker 规范：zip 的 v2 签名要含目录条目（与 0.12 一致），
    新容器没有目录条目、按内容条目签。"""

    def test_zip_dir_marker_is_signed(self):
        path = Path(tempfile.mkdtemp(prefix="pe-sig-")) / "d.eapack"
        self.addCleanup(shutil.rmtree, path.parent, True)
        w = PackWriter(path, use_v2=False)
        w.add_bytes("a.txt", b"x")
        w.add_dir_marker("empty/")
        manifest = w.finish({}, {"format_version": 1}, tamper_proof=True)
        self.assertEqual(manifest["signature_version"], 2)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        self.assertIn("empty/", names)
        zentries = zip_content_entries_v2(path)
        self.assertIn("empty/", [n for n, _s, _h in zentries])
        self.assertEqual(compute_signature_v2({}, json.loads(
            zipfile.ZipFile(path).read("ea_manifest.json")), zentries),
            manifest["signature"])
        # 新容器口径排除目录条目（没有目录 marker 可签）
        self.assertNotIn("empty/", [n for n, _s, _h in content_entries(path)])


class OldSignatureReadCase(unittest.TestCase):
    def test_v1_zip_still_verifies(self):
        tmp = Path(tempfile.mkdtemp(prefix="pe-v1-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "v1.eapack"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.txt", b"hello")
            zf.writestr("dir/", b"")
        manifest = {"format_version": 1, "signature_version": 1}
        manifest["signature"] = compute_zip_signature_v1(path, {}, manifest)
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ea_manifest.json", json.dumps(manifest))
        ok, reason = eapack_mod.verify_signature(path)
        self.assertTrue(ok, reason)


class ExportCompatCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-super-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._cfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._cfg.start()
        self.addCleanup(self._cfg.stop)
        db.set_db_path(dbdir)
        self.pack = self.tmp / "versions" / "P"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c = 1\n")

    def _export(self, name: str, opts: dict) -> Path:
        out = self.tmp / name
        res = instance_export.export_from_instance(
            self.pack, out,
            rules=export_rules.expand_rules(export_rules.default_keys()),
            strategies={"mods": "完全匹配"}, whitelist=["mods"], opts=opts,
            cf_key="", lookup_fn=lambda wanted: {})
        self.assertTrue(res and out.is_file(), res)
        return out

    def test_default_is_legacy_zip_with_v2_signature(self):
        out = self._export("old.eapack", {"super_compress": False})
        self.assertEqual(out.read_bytes()[:2], b"PK")
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("ea_manifest.json"))
            self.assertEqual(manifest.get("signature_version"), 2)
        ok, reason = eapack_mod.verify_signature(out)
        self.assertTrue(ok, reason)

    def test_super_is_eapk2_and_verifies(self):
        out = self._export("super.eapack", {"super_compress": True})
        self.assertEqual(out.read_bytes()[:5], b"EAPK2")
        with open_container(out) as reader:
            manifest = json.loads(reader.read("ea_manifest.json"))
        self.assertEqual(manifest.get("signature_version"), 2)
        ok, reason = eapack_mod.verify_signature(out)
        self.assertTrue(ok, reason)

    def test_new_client_reads_legacy_metadata(self):
        legacy = self.tmp / "legacy.eapack"
        with zipfile.ZipFile(legacy, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "name": "Old",
                "versionId": "1.0.0", "files": []}))
            zf.writestr("ea_settings.json", json.dumps({
                "format_version": 1,
                "recommended_strategies": {"mods": "完全匹配"}}))
            zf.writestr("ea_manifest.json", json.dumps({
                "format_version": 1, "pack_name": "Old",
                "pack_version": "1.0.0", "source": "instance"}))
        self.assertEqual(eapack_mod.read_settings(legacy)["recommended_strategies"],
                         {"mods": "完全匹配"})
        self.assertEqual(eapack_mod.read_manifest(legacy).get("pack_name"), "Old")


class SavesTransformCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-saves-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._cfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._cfg.start()
        self.addCleanup(self._cfg.stop)
        db.set_db_path(dbdir)
        self.pack = self.tmp / "versions" / "S"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c = 1\n")
        self.world = self.pack / "saves" / "World"
        self.region = make_mca()
        _write(self.world / "region" / "r.0.0.mca", self.region)
        self.poi = make_mca([(2, b"poi" * 500)])
        _write(self.world / "poi" / "r.0.0.mca", self.poi)
        self.dim = make_mca([(3, b"raw" * 400)])
        _write(self.world / "DIM-1" / "region" / "r.-1.0.mca", self.dim)
        _write(self.world / "level.dat", b"LEVEL" * 100)
        # 不应该被变换的 region（不在 saves 下）
        self.other = make_mca()
        _write(self.pack / "mods" / "x" / "region" / "r.0.0.mca", self.other)
        self.corrupt = b"not-an-mca" + os.urandom(64)
        # gzip 区块重打包后头部（mtime）不一定逐字节相同：必须安全回退
        self.gzip_world = make_mca([(1, b"gzip" * 300)])
        _write(self.world / "region" / "bad.mca", self.corrupt)
        _write(self.world / "region" / "gz.mca", self.gzip_world)

    def _export(self, name: str, super_on: bool) -> Path:
        rules = export_rules.expand_rules(export_rules.default_keys() | {"saves"})
        out = self.tmp / name
        res = instance_export.export_from_instance(
            self.pack, out, rules=rules,
            strategies={"mods": "完全匹配", "saves": "完全匹配"},
            whitelist=["mods", "config"], opts={"super_compress": super_on},
            cf_key="", lookup_fn=lambda wanted: {})
        self.assertTrue(res and out.is_file(), res)
        return out

    def test_super_transforms_only_saves_regions(self):
        out = self._export("s.eapack", True)
        with open_container(out) as r:
            h = json.loads(r.read("ea_hashes.json"))
            self.assertEqual(set(h.get("saves_worlds") or {}),
                             {"World"})
            self.assertNotIn("saves", h.get("folders") or {})
            reg = r.entries["overrides/saves/World/region/r.0.0.mca"]
            poi = r.entries["overrides/saves/World/poi/r.0.0.mca"]
            dim = r.entries["overrides/saves/World/DIM-1/region/r.-1.0.mca"]
            bad = r.entries["overrides/saves/World/region/bad.mca"]
            gz = r.entries["overrides/saves/World/region/gz.mca"]
            other = r.entries["overrides/mods/x/region/r.0.0.mca"]
            self.assertEqual(reg.get("transform"), "mca")
            self.assertEqual(poi.get("transform"), "mca")
            self.assertEqual(dim.get("transform"), "mca")
            # 损坏文件：变换失败 → 原样存，不报错
            self.assertIsNone(bad.get("transform"))
            self.assertIsNone(gz.get("transform"))   # 不可逐字节还原 → 原样
            # saves 之外的 region 一律不动
            self.assertIsNone(other.get("transform"))
            for name, raw in [
                ("overrides/saves/World/region/r.0.0.mca", self.region),
                ("overrides/saves/World/poi/r.0.0.mca", self.poi),
                ("overrides/saves/World/DIM-1/region/r.-1.0.mca", self.dim),
                ("overrides/saves/World/region/bad.mca", self.corrupt),
                ("overrides/saves/World/region/gz.mca", self.gzip_world),
                ("overrides/mods/x/region/r.0.0.mca", self.other),
            ]:
                got = r.read(name)
                self.assertEqual(hashlib.sha1(got).hexdigest(),
                                 hashlib.sha1(raw).hexdigest(), name)

    def test_off_keeps_saves_byte_exact_untransformed(self):
        out = self._export("z.eapack", False)
        self.assertEqual(out.read_bytes()[:2], b"PK")
        with open_container(out) as r:
            ent = r.entries["overrides/saves/World/region/r.0.0.mca"]
            self.assertIsNone(ent.get("transform"))
            self.assertEqual(r.read("overrides/saves/World/region/r.0.0.mca"),
                             self.region)

    def _make_src_zip(self):
        src = self.tmp / "src"
        _write(src / "modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "name": "P",
            "versionId": "1.0.0", "files": [],
            "dependencies": {"minecraft": "1.20.1"}}))
        _write(src / "overrides" / "saves" / "World" / "region" / "r.0.0.mca",
               self.region)
        zip_path = self.tmp / "src.mrpack"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())
        return src, zip_path

    def test_zip_source_path_legacy_zip_v2(self):
        src, zip_path = self._make_src_zip()
        from app.core.mrpack import merge_files, parse_mrpack
        pack, err = parse_mrpack(zip_path)
        self.assertIsNotNone(pack, err)
        out = self.tmp / "fromzip-old.eapack"
        res = eapack_mod.export_eapack(
            source_dir=src, source_zip=zip_path, out_path=out, pack=pack,
            merged_files=merge_files(pack), strategies={"saves": "完全匹配"},
            changelog_md="", opts={"super_compress": False})
        self.assertTrue(res and out.is_file(), res)
        self.assertEqual(out.read_bytes()[:2], b"PK")
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("ea_manifest.json"))
            hashes = json.loads(zf.read("ea_hashes.json"))
            self.assertEqual(manifest.get("signature_version"), 2)
            # 旧客户端只认顶层键：必须只有 saves，不能有 saves/World
            self.assertIn("saves", hashes["folders"])
            self.assertFalse([k for k in hashes["folders"]
                              if k.startswith("saves/")])
        ok, reason = eapack_mod.verify_signature(out)
        self.assertTrue(ok, reason)

    def test_zip_source_path_honours_super(self):
        # 从"启动器导出的 zip"重新打包这条路径
        src = self.tmp / "src"
        _write(src / "modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "name": "P",
            "versionId": "1.0.0", "files": [],
            "dependencies": {"minecraft": "1.20.1"}}))
        _write(src / "overrides" / "saves" / "World" / "region" / "r.0.0.mca",
               self.region)
        zip_path = self.tmp / "src.mrpack"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())
        from app.core.mrpack import merge_files, parse_mrpack
        pack, err = parse_mrpack(zip_path)
        self.assertIsNotNone(pack, err)
        merged = merge_files(pack)
        out = self.tmp / "fromzip.eapack"
        res = eapack_mod.export_eapack(
            source_dir=src, source_zip=zip_path, out_path=out, pack=pack,
            merged_files=merged, strategies={"saves": "完全匹配"},
            changelog_md="", opts={"super_compress": True})
        self.assertTrue(res and out.is_file(), res)
        with open_container(out) as r:
            ent = r.entries["overrides/saves/World/region/r.0.0.mca"]
            self.assertEqual(ent.get("transform"), "mca")
            self.assertEqual(hashlib.sha1(r.read(
                "overrides/saves/World/region/r.0.0.mca")).hexdigest(),
                hashlib.sha1(self.region).hexdigest())


if __name__ == "__main__":
    unittest.main()


class RestoreBoundsCase(unittest.TestCase):
    """恶意 mca 变换流：只允许快速 ValueError，不许按声明值巨量分配。"""

    def test_huge_file_len_rejected(self):
        blob = mca._MAGIC + struct.pack(">II", 0xFFFFFFFF, 0) + b"\x00" * 8192
        with self.assertRaises(ValueError):
            mca.restore_region(blob)

    def test_small_blob_cannot_claim_1gib(self):
        blob = mca._MAGIC + struct.pack(">II", 1 << 30, 0) + b"\x00" * 8192
        with self.assertRaises(ValueError):
            mca.restore_region(blob)

    def test_huge_raw_len_rejected(self):
        hdr = bytearray(8192)
        hdr[0:3] = (2).to_bytes(3, "big")
        hdr[3] = 1
        blob = (mca._MAGIC + struct.pack(">II", 8192 + 4096, 1) + bytes(hdr)
                + bytes([2]) + b"\xff\xff\xff\xff\x0f")
        with self.assertRaises(ValueError):
            mca.restore_region(blob)

    def test_slot_out_of_range_rejected(self):
        hdr = bytearray(8192)
        hdr[0:3] = (0xFFFFFF).to_bytes(3, "big")
        hdr[3] = 1
        blob = mca._MAGIC + struct.pack(">II", 8192 + 4096, 1) + bytes(hdr)
        with self.assertRaises(ValueError):
            mca.restore_region(blob)

    def test_gap_count_bomb_rejected(self):
        hdr = bytes(8192)
        blob = (mca._MAGIC + struct.pack(">II", 8192, 0) + hdr
                + b"\xff\xff\xff\xff\x0f")
        with self.assertRaises(ValueError):
            mca.restore_region(blob)

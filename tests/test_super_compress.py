"""超级压缩（beta）开关 + 旧版格式互通。

* super_compress 开启 → 新版容器 + 存档变换 + zstd 22；
* 关闭 compact_container → 导出旧版 zip，旧客户端/zipfile 都能读；
* 新客户端能读旧版 zip 更新包（解析入口统一走 open_container）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.core import eapack as eapack_mod
from app.core import export_rules
from app.core import instance_export
from app.core.container import open_container
from app.core.packwriter import resolve_export_options


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ResolveOptionsCase(unittest.TestCase):
    def test_super_forces_v2_mca_level22(self):
        got = resolve_export_options({"compact_container": False,
                                      "world_transform": False,
                                      "super_compress": True})
        self.assertTrue(got["use_v2"])
        self.assertTrue(got["mca"])
        self.assertEqual(got["level"], 22)

    def test_legacy_zip_when_compact_off(self):
        got = resolve_export_options({"compact_container": False,
                                      "world_transform": False,
                                      "super_compress": False})
        self.assertFalse(got["use_v2"])
        self.assertFalse(got["mca"])
        self.assertEqual(got["level"], 17)


class SuperCompressExportCase(unittest.TestCase):
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

    def test_legacy_zip_is_a_real_zip(self):
        out = self._export("old.eapack", {"compact_container": False,
                                          "super_compress": False,
                                          "world_transform": False})
        self.assertEqual(out.read_bytes()[:2], b"PK")
        with zipfile.ZipFile(out) as zf:
            self.assertIn("modrinth.index.json", zf.namelist())
        with open_container(out) as reader:
            self.assertIn("modrinth.index.json", reader.names)

    def test_super_compress_writes_v2_and_reads_back(self):
        out = self._export("super.eapack", {"super_compress": True})
        self.assertEqual(out.read_bytes()[:5], b"EAPK2")
        with open_container(out) as reader:
            self.assertIn("modrinth.index.json", reader.names)

    def test_legacy_pack_metadata_parses(self):
        # 手工造一个 0.12 风格的 zip eapack（没有容器头），新版要能读
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
        settings = eapack_mod.read_settings(legacy)
        manifest = eapack_mod.read_manifest(legacy)
        rec = eapack_mod.read_recommended_strategies(legacy)
        self.assertEqual(settings.get("recommended_strategies"),
                         {"mods": "完全匹配"})
        self.assertEqual(manifest.get("pack_name"), "Old")
        self.assertEqual(rec, {"mods": "完全匹配"})


if __name__ == "__main__":
    unittest.main()

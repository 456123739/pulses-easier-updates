"""从本地实例导出 .eapack：结构、index、overtides、取消、读回。"""

import json
import tempfile
import threading
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from app.core import eapack, export_rules as R, instance_export as EX
from app.core.lookup import LookupHit


def build_instance(root: Path) -> None:
    (root / "mods").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "screenshots").mkdir()
    (root / "mods" / "remote.jar").write_bytes(b"R" * 4096)
    (root / "mods" / "local.jar").write_bytes(b"L" * 2048)
    (root / "config" / "app.toml").write_text("k = 1\n", encoding="utf-8")
    (root / "screenshots" / "shot.png").write_bytes(b"P" * 100)
    (root / "saves" / "world").mkdir(parents=True)
    (root / "saves" / "world" / "level.dat").write_bytes(b"D" * 64)
    (root / "options.txt").write_text("fov:0.5\n", encoding="utf-8")
    (root / f"{root.name}.json").write_text(json.dumps({
        "id": "1.20.1-forge-47.2.0", "inheritsFrom": "1.20.1-forge-47.2.0",
    }), encoding="utf-8")


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-exp-"))
        self.root = self.tmp / "MyPack"
        self.root.mkdir()
        build_instance(self.root)
        self.out = self.tmp / "out" / "pack.eapack"

    def run_export(self, **kwargs):
        def fake_lookup(wanted):
            return {"mods/remote.jar": LookupHit(
                source="curseforge",
                urls=["https://edge.forgecdn.net/files/1/2/remote.jar"],
                file_name="remote.jar", size=4096, project="demo",
                version="1.0")}

        return EX.export_from_instance(
            self.root, self.out,
            rules=R.expand_rules(),
            strategies={"config": "保留键位"},
            whitelist=["config", "mods"],
            changelog_md="## 1.0.0\n- 首个版本\n",
            pack_name="MyPack", pack_version="1.0.0", summary="测试包",
            opts={"include_hashes": True},
            lookup_fn=kwargs.pop("lookup_fn", fake_lookup),
            **kwargs)

    def test_export_ok_and_structure(self):
        path = self.run_export()
        self.assertEqual(path, self.out)
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            self.assertIn("modrinth.index.json", names)
            self.assertIn("ea_settings.json", names)
            self.assertIn("ea_manifest.json", names)
            self.assertIn("ea_hashes.json", names)
            self.assertIn("changelog.md", names)
            self.assertIn("overrides/mods/local.jar", names)
            self.assertFalse(any(n.startswith("overrides/screenshots") for n in names))

            index = json.loads(zf.read("modrinth.index.json"))
            self.assertEqual(len(index["files"]), 1)
            entry = index["files"][0]
            self.assertEqual(entry["path"], "mods/remote.jar")
            self.assertEqual(entry["downloads"],
                             ["https://edge.forgecdn.net/files/1/2/remote.jar"])
            self.assertEqual(index["dependencies"],
                             {"minecraft": "1.20.1", "forge": "47.2.0"})
            self.assertEqual(index["versionId"], "1.0.0")

    def test_settings_and_manifest_readback(self):
        path = self.run_export()
        settings = eapack.read_settings(path)
        self.assertEqual(settings["recommended_strategies"], {"config": "保留键位"})
        self.assertEqual(settings["whitelist"], ["config", "mods"])
        manifest = eapack.read_manifest(path)
        self.assertEqual(manifest["source"], "instance")
        self.assertEqual(manifest["pack_name"], "MyPack")
        self.assertEqual(manifest["dependencies"]["forge"], "47.2.0")
        self.assertTrue(manifest["has_changelog"])
        self.assertEqual(eapack.read_changelog(path), "## 1.0.0\n- 首个版本\n")

    def test_hashes_cover_overrides_and_index(self):
        path = self.run_export()
        data = eapack.read_hashes(path)
        files = data["files"]
        self.assertIn("mods/local.jar", files)
        self.assertIn("config/app.toml", files)
        self.assertNotIn("screenshots/shot.png", files)
        self.assertEqual(len(files["mods/local.jar"]["value"]), 32)
        self.assertEqual(files["mods/local.jar"]["algo"], "blake2b")

    def test_hit_without_url_falls_back_to_local(self):
        from app.core.lookup import LookupHit
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            lookup_fn=lambda wanted: {"mods/local.jar": LookupHit(
                source="modrinth", urls=[""], file_name="local.jar", size=2048)})
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            index = json.loads(zf.read("modrinth.index.json"))
        self.assertEqual(index["files"], [])
        self.assertIn("overrides/mods/local.jar", names)

    def test_error_during_zip_leaves_no_part_file(self):
        import zipfile as zf_mod
        with mock.patch.object(zf_mod.ZipFile, "write",
                               side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                EX.export_from_instance(self.root, self.out,
                                        rules=R.expand_rules(),
                                        lookup_fn=lambda wanted: {})
        self.assertFalse(self.out.exists())
        leftovers = [p.name for p in self.out.parent.glob("*.part-*")]
        self.assertEqual(leftovers, [])

    def test_signature_v2_detects_content_change(self):
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            opts={"tamper_proof": True}, lookup_fn=lambda wanted: {})
        manifest = eapack.read_manifest(path)
        self.assertEqual(manifest.get("signature_version"), 2)
        self.assertEqual(eapack.verify_signature(path), (True, ""))

        # 改了内容但保持长度：v1（CRC）可能漏，v2（内容 sha1）必须发现
        import shutil
        bad = self.tmp / "bad.eapack"
        shutil.copy(path, bad)
        with zipfile.ZipFile(bad) as src, zipfile.ZipFile(
                self.tmp / "new.eapack", "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "overrides/mods/local.jar":
                    data = b"X" * len(data)
                dst.writestr(item, data)
        ok, reason = eapack.verify_signature(self.tmp / "new.eapack")
        self.assertFalse(ok)

    def test_unreadable_file_fails_export(self):
        # 用注入的读失败模拟"文件读不出来"：Windows 上 chmod 000 对属主无效，
        # 靠权限位造不出这个场景。
        from app.core import fingerprint as fpmod
        secret = self.root / "mods" / "secret.jar"
        secret.write_bytes(b"S" * 64)
        real = fpmod.FingerprintCache.compute

        def fake_compute(cache, path, **kwargs):
            if Path(path).name == "secret.jar":
                raise PermissionError("denied")
            return real(cache, path, **kwargs)

        with mock.patch.object(fpmod.FingerprintCache, "compute", fake_compute):
            path = EX.export_from_instance(self.root, self.out,
                                           rules=R.expand_rules(),
                                           lookup_fn=lambda wanted: {})
        self.assertIsNone(path)
        self.assertFalse(self.out.exists())

    def test_signature_failure_aborts(self):
        with mock.patch.object(eapack, "_content_entries",
                               side_effect=OSError("read fail")):
            with self.assertRaises(OSError):
                EX.export_from_instance(self.root, self.out,
                                        rules=R.expand_rules(),
                                        opts={"tamper_proof": True},
                                        lookup_fn=lambda wanted: {})
        self.assertFalse(self.out.exists())

    def test_zero_byte_file_is_packed(self):
        (self.root / "config" / "empty.toml").write_bytes(b"")
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            lookup_fn=lambda wanted: {})
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        self.assertIn("overrides/config/empty.toml", names)

    def test_cancel_returns_none_without_output(self):
        cancel = threading.Event()

        def lookup_then_cancel(wanted):
            cancel.set()
            return {}

        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            cancel=cancel, lookup_fn=lookup_then_cancel)
        self.assertIsNone(path)
        self.assertFalse(self.out.exists())

    def test_rules_respected(self):
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules({"saves"}),
            lookup_fn=lambda wanted: {})
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        self.assertIn("overrides/saves/world/level.dat", names)
        self.assertNotIn("overrides/screenshots/shot.png", names)
        self.assertFalse(any(n.startswith("overrides/mods/") for n in names))

    def test_chinese_paths_and_options_excluded(self):
        (self.root / "config" / "中文配置.toml").write_text("a=1\n", encoding="utf-8")
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            lookup_fn=lambda wanted: {}, whitelist=["config"])
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            self.assertTrue(any("中文配置.toml" in n for n in names))
            self.assertNotIn("overrides/options.txt", names)
            self.assertTrue(all(n.isprintable() for n in names))

    def test_excluded_tops_recorded_in_settings(self):
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            excluded_tops=["mods"], lookup_fn=lambda wanted: {})
        settings = eapack.read_settings(path)
        self.assertEqual(settings["excluded_folders"], ["mods"])

    def test_no_lookup_still_exports_local(self):
        path = EX.export_from_instance(
            self.root, self.out, rules=R.expand_rules(),
            lookup_fn=lambda wanted: {})
        with zipfile.ZipFile(path) as zf:
            index = json.loads(zf.read("modrinth.index.json"))
        self.assertEqual(index["files"], [])
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()

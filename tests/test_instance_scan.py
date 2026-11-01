"""实例扫描：跳过规则、候选判定、加载器探测。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.core import export_rules as R
from app.core import instance_scan as S


def make_tree(root: Path) -> None:
    (root / "mods").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "mods" / "a.jar").write_bytes(b"a" * 100)
    (root / "mods" / "b.jar.disabled").write_bytes(b"b" * 10)
    (root / "mods" / "sub").mkdir(exist_ok=True)
    (root / "mods" / "sub" / "c.zip").write_bytes(b"c" * 5)
    (root / "config" / "x.toml").write_text("k=1", encoding="utf-8")
    (root / "screenshots").mkdir(exist_ok=True)
    (root / "screenshots" / "s.png").write_bytes(b"s")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "big").write_bytes(b"x")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("log", encoding="utf-8")


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-scan-"))
        make_tree(self.tmp)

    def test_default_rules(self):
        res = S.scan_instance(self.tmp, R.expand_rules())
        names = {r["name"] for r in res["rows"]}
        self.assertEqual(names, {"mods", "config"})
        self.assertEqual(res["totals"]["files"], 3)   # a.jar + sub/c.zip + config
        self.assertEqual(res["totals"]["candidates"], 2)

    def test_disabled_included_when_selected(self):
        res = S.scan_instance(self.tmp, R.expand_rules({"mods", "mods-disabled"}))
        paths = {i["path"] for i in res["items"]}
        self.assertIn("mods/b.jar.disabled", paths)

    def test_skip_dirs_never_scanned(self):
        res = S.scan_instance(self.tmp, R.expand_rules({"screenshots"}))
        paths = {i["path"] for i in res["items"]}
        self.assertFalse(any(p.startswith("assets/") for p in paths))
        self.assertFalse(any(p.startswith("logs/") for p in paths))
        self.assertIn("screenshots/s.png", paths)

    def test_candidate_rule(self):
        self.assertTrue(S.is_candidate("mods/a.jar"))
        self.assertTrue(S.is_candidate("resourcepacks/p.zip"))
        self.assertTrue(S.is_candidate("openloader/x/y.zip"))
        self.assertFalse(S.is_candidate("config/a.toml"))
        self.assertFalse(S.is_candidate("screenshots/s.png"))

    def test_loader_detection(self):
        name = self.tmp.name
        (self.tmp / f"{name}.json").write_text(json.dumps({
            "id": "1.20.1-fabric-0.15.11",
            "inheritsFrom": "fabric-loader-0.15.11-1.20.1",
        }), encoding="utf-8")
        info = S.detect_loader(self.tmp)
        self.assertEqual(info["loader"], "fabric")
        self.assertEqual(info["minecraft"], "1.20.1")
        self.assertEqual(info["loaderVersion"], "0.15.11")

    def test_symlink_escape_is_blocked_and_reported(self):
        import os as _os
        outside = self.tmp.parent / (self.tmp.name + "-secret")
        outside.mkdir()
        (outside / "id_rsa").write_bytes(b"KEY")
        try:
            _os.symlink(outside, self.tmp / "config" / "creds",
                        target_is_directory=True)
        except (OSError, NotImplementedError):
            import shutil as _sh
            _sh.rmtree(outside, ignore_errors=True)
            self.skipTest("这个平台不允许创建目录软链接")
        try:
            res = S.scan_instance(self.tmp, R.expand_rules())
            paths = {i["path"] for i in res["items"]}
            self.assertFalse(any("id_rsa" in p for p in paths))
            self.assertTrue(any("creds" in u for u in res["unsafeLinks"]))
        finally:
            (self.tmp / "config" / "creds").unlink()
            import shutil as _sh
            _sh.rmtree(outside, ignore_errors=True)

    def test_multi_version_does_not_guess(self):
        import json as _json
        mc = self.tmp / "mc"
        (mc / "mods").mkdir(parents=True)
        (mc / "config").mkdir()
        for ver, payload in (
                ("1.12.2-forge", {"id": "1.12.2-forge-14.23.5.2859",
                                  "inheritsFrom": "1.12.2"}),
                ("1.20.1-fabric", {"id": "fabric-loader-0.15.11-1.20.1",
                                   "inheritsFrom": "1.20.1"})):
            (mc / "versions" / ver).mkdir(parents=True)
            (mc / "versions" / ver / f"{ver}.json").write_text(
                _json.dumps(payload), encoding="utf-8")
        info = S.detect_loader(mc)
        self.assertEqual(info["minecraft"], "")

    def test_loader_forge(self):
        name = self.tmp.name
        (self.tmp / f"{name}.json").write_text(json.dumps({
            "id": "1.20.1-forge-47.2.0",
            "inheritsFrom": "1.20.1-forge-47.2.0",
        }), encoding="utf-8")
        info = S.detect_loader(self.tmp)
        self.assertEqual(info["loader"], "forge")
        self.assertEqual(info["loaderVersion"], "47.2.0")

    def test_symlinked_mods_dir_is_followed(self):
        import os as _os
        real = self.tmp.parent / (self.tmp.name + "-mods")
        real.mkdir()
        (real / "linked.jar").write_bytes(b"L" * 16)
        import shutil as _sh
        _sh.rmtree(self.tmp / "mods")
        try:
            _os.symlink(real, self.tmp / "mods", target_is_directory=True)
        except (OSError, NotImplementedError):
            _sh.rmtree(real, ignore_errors=True)
            make_tree(self.tmp)
            self.skipTest("这个平台不允许创建目录软链接")
        try:
            res = S.scan_instance(self.tmp, R.expand_rules())
            paths = {i["path"] for i in res["items"]}
            self.assertIn("mods/linked.jar", paths)
        finally:
            (self.tmp / "mods").unlink()
            _sh.rmtree(real, ignore_errors=True)
            make_tree(self.tmp)

    def test_symlink_cycle_terminates(self):
        import os as _os
        try:
            _os.symlink(self.tmp, self.tmp / "mods" / "loop",
                        target_is_directory=True)
        except OSError:
            self.skipTest("不支持软链接")
        res = S.scan_instance(self.tmp, R.expand_rules())
        self.assertLess(res["totals"]["files"], 50)

    def test_loader_versions_pcl_style(self):
        import json as _json
        (self.tmp / "versions" / "1.20.1-Fabric").mkdir(parents=True)
        (self.tmp / "versions" / "1.20.1-Fabric" / "1.20.1-Fabric.json"
         ).write_text(_json.dumps({
             "id": "1.20.1-Fabric",
             "inheritsFrom": "fabric-loader-0.15.11-1.20.1",
         }), encoding="utf-8")
        info = S.detect_loader(self.tmp)
        self.assertEqual(info["loader"], "fabric")
        self.assertEqual(info["minecraft"], "1.20.1")
        self.assertEqual(info["loaderVersion"], "0.15.11")

    def test_loader_forge_packed_version(self):
        import json as _json
        (self.tmp / f"{self.tmp.name}.json").write_text(_json.dumps({
            "id": "1.12.2-forge1.12.2-14.23.5.2859",
            "inheritsFrom": "1.12.2",
        }), encoding="utf-8")
        info = S.detect_loader(self.tmp)
        self.assertEqual(info["loader"], "forge")
        self.assertEqual(info["minecraft"], "1.12.2")
        self.assertEqual(info["loaderVersion"], "14.23.5.2859")

    def test_locate_requires_mods_and_config(self):
        import os
        bare = Path(tempfile.mkdtemp(prefix="pe-bare-"))
        try:
            with self.assertRaises(FileNotFoundError):
                S.locate_and_scan(bare)
        finally:
            os.rmdir(bare)
        res = S.locate_and_scan(self.tmp, R.expand_rules())
        self.assertEqual(Path(res["root"]).name, self.tmp.name)


if __name__ == "__main__":
    unittest.main()

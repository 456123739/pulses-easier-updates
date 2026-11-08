"""特殊导出内容：扫到了才出现，默认都不导出。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import export_rules
from app.core import export_specials


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DetectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-special-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "Pack"
        (self.root / "mods").mkdir(parents=True)
        (self.root / "config").mkdir()
        _write(self.root / "mods" / "normal.jar", "J")

    def keys(self) -> list[str]:
        return [item["key"] for item in export_specials.detect(self.root)]

    def test_empty_instance_has_none(self):
        self.assertEqual(self.keys(), [])

    def test_not_fooled_by_unrelated_files(self):
        _write(self.root / "config" / "jei" / "other.toml", "x")
        _write(self.root / "mods" / "disabled.txt", "x")
        _write(self.root / "screenshots2" / "a.png", "x")
        self.assertEqual(self.keys(), [])

    def test_optifine(self):
        _write(self.root / "optionsof.txt", "fov:90")
        found = export_specials.detect(self.root)
        self.assertEqual([f["key"] for f in found], ["optifine"])
        self.assertEqual(found[0]["files"], 1)
        self.assertEqual(found[0]["bytes"], len("fov:90"))
        self.assertEqual(found[0]["sample"], ["optionsof.txt"])

    def test_disabled_mods(self):
        _write(self.root / "mods" / "sodium.jar.disabled", "x")
        _write(self.root / "mods" / "old.jar.old", "x")
        found = export_specials.detect(self.root)
        self.assertEqual([f["key"] for f in found], ["mods-disabled"])
        self.assertEqual(found[0]["files"], 2)

    def test_nested_dirs(self):
        _write(self.root / "config" / "jei" / "world" / "bookmarks.ini", "x")
        _write(self.root / "journeymap" / "data" / "sp" / "world" / "a.dat", "x")
        _write(self.root / "screenshots" / "2026-01-01.png", "x")
        self.assertEqual(self.keys(), ["jei", "maps", "screenshots"])

    def test_order_follows_specials(self):
        _write(self.root / "replay_recordings" / "a.mcpr", "x")
        _write(self.root / "emi.json", "{}")
        _write(self.root / "optionsshaders.txt", "x")
        self.assertEqual(self.keys(), ["optifine", "emi", "replay"])

    def test_does_not_read_file_contents(self):
        _write(self.root / "screenshots" / "a.png", "x")
        real_stat = Path.stat
        reads: list[str] = []

        def spy(self_path, *a, **kw):             # noqa: ANN001
            return real_stat(self_path, *a, **kw)

        with mock.patch.object(Path, "read_bytes", lambda *a, **k: reads.append("read")), \
                mock.patch.object(Path, "read_text", lambda *a, **k: reads.append("read")), \
                mock.patch.object(Path, "open", lambda *a, **k: reads.append("open")):
            found = export_specials.detect(self.root)
        self.assertEqual([f["key"] for f in found], ["screenshots"])
        self.assertEqual(reads, [], "detect 不该读文件内容")

    def test_cap(self):
        for i in range(12):
            _write(self.root / "screenshots" / f"s{i}.png", "x")
        with mock.patch.object(export_specials, "MAX_FILES", 5):
            found = export_specials.detect(self.root)
        self.assertEqual(found[0]["files"], 5)

    def test_not_a_directory(self):
        self.assertEqual(export_specials.detect(self.root / "optionsof.txt"), [])

    def test_keys_are_known_to_rule_engine(self):
        """每个特殊选项都必须能在 export_rules 里找到同名 key，否则勾了没效果。"""
        known = {g.key for g in export_rules.DEFAULT_OPTIONS}
        for group in export_rules.DEFAULT_OPTIONS:
            known.update(c.key for c in group.children)
        self.assertTrue(export_specials.keys())
        self.assertTrue(export_specials.keys() <= known,
                        export_specials.keys() - known)

    def test_optifine_is_default_on_in_engine(self):
        """PCL 语义里 optifine 默认是勾的；本项目在 DevFlow 里把它剔掉（见另一个用例）。"""
        self.assertIn("optifine", export_rules.default_keys())


if __name__ == "__main__":
    unittest.main()

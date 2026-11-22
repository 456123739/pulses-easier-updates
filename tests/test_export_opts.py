"""导出选项以调用方为准：zip / 紧凑容器不能只认数据库里的值。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.core import export_rules
from app.core import instance_export


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ExportContainerOptsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-opts-"))
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

    def _export(self, name: str, opts: dict) -> bytes:
        out = self.tmp / name
        res = instance_export.export_from_instance(
            self.pack, out,
            rules=export_rules.expand_rules(export_rules.default_keys()),
            strategies={"mods": "完全匹配"},
            whitelist=["mods"],
            opts=opts, cf_key="", lookup_fn=lambda wanted: {},
        )
        self.assertTrue(res and out.is_file(), res)
        return out.read_bytes()[:16]

    def test_opts_override_db(self):
        db.set_export_options({**db.get_export_options(),
                               "compact_container": False,
                               "world_transform": False})
        self.assertTrue(self._export("v2.eapack",
                                     {"compact_container": True}).startswith(b"EAPK2"))
        self.assertTrue(self._export("zip.eapack",
                                     {"compact_container": False}).startswith(b"PK"))

    def test_db_used_when_opts_missing(self):
        db.set_export_options({**db.get_export_options(),
                               "compact_container": True})
        self.assertTrue(self._export("defaults.eapack", {}).startswith(b"EAPK2"))


if __name__ == "__main__":
    unittest.main()

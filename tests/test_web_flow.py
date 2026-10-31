"""Web 界面（app/ui_web）的流程级测试：导入 → 比对 → 应用。

用真实磁盘搭一个"玩家整合包 + 更新包"，跑 AppController + Flow，
不依赖任何 GUI。Tk 版有等价用例（tests/e2e_flow.py），这里保证两条
界面的流程判定一致。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core import database as db
from app.core import eapack as eapack_mod
from app.core.mrpack import merge_files, parse_mrpack
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class WebFlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-webflow-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        db.set_db_path(dbdir)

        self.inst = self.tmp / "versions" / "DemoPack"
        _write(self.inst / "mods" / "old.jar", "OLD")
        _write(self.inst / "mods" / "local-only.jar", "LOCAL")
        _write(self.inst / "config" / "shipped.toml", 'key = "old"\n')
        _write(self.inst / "config" / "player.toml", "mine = 1\n")
        _write(self.inst / "config" / "sodium-options.json",
               '{"render_distance": 8, "tuned_by": "player"}\n')
        _write(self.inst / "options.txt", "fov:90\n")

        src = self.tmp / "src"
        index = {
            "formatVersion": 1, "game": "minecraft", "versionId": "2.0.0",
            "name": "DemoPack", "files": [],
            "dependencies": {"minecraft": "1.20.1"},
        }
        _write(src / "modrinth.index.json", json.dumps(index, indent=2))
        _write(src / "overrides" / "mods" / "old.jar", "NEW")
        _write(src / "overrides" / "mods" / "added.jar", "ADDED")
        _write(src / "overrides" / "config" / "shipped.toml", 'key = "new"\n')
        _write(src / "overrides" / "config" / "sodium-options.json",
               '{"render_distance": 16, "tuned_by": "pack"}\n')

        src_zip = self.tmp / "DemoPack-2.0.0.zip"
        with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())

        pack, err = parse_mrpack(src_zip)
        assert pack is not None and not err, err
        self.update = self.tmp / "DemoPack-2.0.0.eapack"
        out = eapack_mod.export_eapack(
            source_dir=src, source_zip=src_zip, out_path=self.update,
            pack=pack, merged_files=merge_files(pack),
            strategies={"mods": "完全匹配"},
            changelog_md="# Demo 2.0.0\n\n- 更新\n")
        assert out is not None

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.open_path(str(self.inst))

    def tearDown(self):
        try:
            self.ctl.flow.clear()
        except Exception:                                  # noqa: BLE001, S110
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def _scan(self) -> dict:
        self.api.invoke("update.pick", {"path": str(self.update)})
        self.api.invoke("pack.scan", {})
        thread = self.ctl.flow._thread
        assert thread is not None
        thread.join(60)
        self.assertFalse(self.ctl.flow.busy())
        return self.ctl.flow.snapshot()

    def test_import_and_scan(self):
        snap = self._scan()
        self.assertIsNone(self.ctl.flow.error)
        counts = snap["counts"]
        self.assertEqual(counts["added"], 1)          # mods/added.jar
        self.assertGreaterEqual(counts["modified"], 1)
        self.assertEqual(counts["deleted"], 1)        # mods/local-only.jar

        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertIn("mods", rows)
        self.assertEqual(rows["mods"]["current"], "完全匹配")
        self.assertEqual(rows["config"]["current"], "智能剔除")
        self.assertTrue(rows["mods"]["checked"])

        groups = {g["top"]: g for g in snap["groups"]}
        self.assertIn("mods", groups)
        self.assertIn("config", groups)
        self.assertTrue(any(i["smartExcluded"] for i in groups["config"]["items"]))

    def test_apply_updates_files_and_keeps_player_data(self):
        self._scan()
        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(120)
        report = self.ctl.flow.report
        self.assertIsNotNone(report, self.ctl.flow.error)
        self.assertEqual(report["failed"], 0)

        self.assertEqual((self.inst / "mods" / "old.jar").read_text(), "NEW")
        self.assertEqual((self.inst / "mods" / "added.jar").read_text(), "ADDED")
        self.assertFalse((self.inst / "mods" / "local-only.jar").exists())
        self.assertEqual((self.inst / "config" / "shipped.toml").read_text(),
                         'key = "new"\n')
        # 玩家自己写的 / 智能剔除保护的，一个都不能动
        self.assertEqual((self.inst / "config" / "player.toml").read_text(),
                         "mine = 1\n")
        self.assertEqual(
            (self.inst / "config" / "sodium-options.json").read_text(),
            '{"render_distance": 8, "tuned_by": "player"}\n')

    def test_strategy_api(self):
        self._scan()
        snap = self.api.invoke("update.strategy.set",
                               {"name": "config", "value": "完全匹配"})
        self.assertTrue(snap["ok"])
        rows = {r["name"]: r for r in snap["data"]["strategies"]}
        self.assertEqual(rows["config"]["current"], "完全匹配")

        bad = self.api.invoke("update.strategy.set",
                              {"name": "config", "value": "不存在的策略"})
        self.assertFalse(bad["ok"])
        self.assertIn(bad["error"]["code"], ("ERROR", "INTERNAL"))

    def test_unchecked_folder_is_skipped(self):
        self._scan()
        self.api.invoke("update.strategy.set",
                        {"name": "config", "checked": False})
        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(120)
        self.assertEqual((self.inst / "config" / "shipped.toml").read_text(),
                         'key = "old"\n')
        self.assertEqual((self.inst / "mods" / "old.jar").read_text(), "NEW")


if __name__ == "__main__":
    unittest.main()

"""特殊导出选项与「更新范围」的关系：默认不导出、勾了才进包、zip 来源没有这些。"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DevSpecialsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-devspec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        self.pack = self.tmp / "versions" / "Pack"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c")
        # 特殊内容
        _write(self.pack / "optionsof.txt", "fov:90\n")
        _write(self.pack / "mods" / "off.jar.disabled", "D")
        _write(self.pack / "config" / "jei" / "world" / "bookmarks.ini", "B")
        _write(self.pack / "screenshots" / "2026-01-01.png", "P")

        self.zip = self.tmp / "Pack.mrpack"
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "name": "Pack",
                "versionId": "1.0.0", "files": [],
                "dependencies": {"minecraft": "1.20.1"}}))
            zf.writestr("overrides/mods/a.jar", "A")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.set_role("developer")

    def dev(self) -> dict:
        return self.api.invoke("dev.state", {})["data"]

    def wait_idle(self, timeout: float = 10.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            snap = self.dev()
            if snap["phase"] != "loading":
                return snap
            time.sleep(0.02)
        self.fail(f"识别没结束：{snap}")

    def open(self, path) -> dict:
        res = self.api.invoke("dev.open", {"path": str(path)})
        self.assertTrue(res["ok"], res)
        return self.wait_idle()

    def names(self, snap: dict) -> set[str]:
        return {row["name"] for row in snap["strategies"]}

    # ------------------------------------------------------------------
    def test_specials_only_when_found_and_all_off(self):
        snap = self.open(self.pack)
        keys = [s["key"] for s in snap["specials"]]
        self.assertEqual(keys, ["optifine", "mods-disabled", "jei", "screenshots"])
        self.assertTrue(all(s["checked"] is False for s in snap["specials"]))
        self.assertEqual(snap["specials"][0]["files"], 1)
        # 默认不导出，但要在第一步里看得见（以 excluded 标记），不能悄悄消失
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertIn("optionsof.txt", rows)
        self.assertTrue(rows["optionsof.txt"]["excluded"])
        self.assertIn("screenshots", rows)
        self.assertTrue(rows["screenshots"]["excluded"])

    def test_enabling_optifine_brings_file_into_scope(self):
        before = self.open(self.pack)
        files_before = before["instance"]["totals"]["files"]

        snap = self.api.invoke("dev.rule.set", {"key": "optifine", "value": True})["data"]
        self.assertIn("optionsof.txt", self.names(snap))
        self.assertEqual(snap["instance"]["totals"]["files"], files_before + 1)
        self.assertTrue(next(s for s in snap["specials"] if s["key"] == "optifine")["checked"])

        # 关掉又回到原样（不会残留）
        snap = self.api.invoke("dev.rule.set", {"key": "optifine", "value": False})["data"]
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertIn("optionsof.txt", rows)
        self.assertTrue(rows["optionsof.txt"]["excluded"])
        self.assertEqual(snap["instance"]["totals"]["files"], files_before)

    def test_enabling_screenshots_adds_row(self):
        self.open(self.pack)
        snap = self.api.invoke("dev.rule.set", {"key": "screenshots", "value": True})["data"]
        self.assertIn("screenshots", self.names(snap))
        self.assertEqual(next(s for s in snap["specials"] if s["key"] == "screenshots")["files"], 1)

    def test_disabled_mod_is_not_a_row_until_enabled(self):
        self.open(self.pack)
        # mods 行在（普通 mod 会导出），但 .disabled 不算
        snap = self.dev()
        self.assertIn("mods", self.names(snap))
        snap = self.api.invoke("dev.rule.set", {"key": "mods-disabled", "value": True})["data"]
        mods = next(r for r in snap["strategies"] if r["name"] == "mods")
        self.assertTrue(mods)  # 仍在
        # 打开后文件数应该 +1
        self.assertGreaterEqual(snap["instance"]["totals"]["files"], 2)

    def test_zip_source_has_no_specials(self):
        snap = self.open(self.zip)
        self.assertEqual(snap["mode"], "zip")
        self.assertEqual(snap["specials"], [])

    def test_clear_drops_specials(self):
        self.open(self.pack)
        snap = self.api.invoke("dev.clear", {})["data"]
        self.assertEqual(snap["specials"], [])
        self.assertIsNone(snap["instance"])


if __name__ == "__main__":
    unittest.main()

"""开发者侧（导出更新包）+ 白名单/数据库 + 续传 的 Web 后端测试。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core import database as db
from app.core import eapack as eapack_mod
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DevExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-dev-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        db.set_db_path(dbdir)

        src = self.tmp / "src"
        _write(src / "modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "versionId": "5.0.0",
            "name": "DevPack", "files": [],
            "dependencies": {"minecraft": "1.20.1"}}))
        _write(src / "overrides" / "mods" / "a.jar", "A")
        _write(src / "overrides" / "config" / "c.toml", "c = 1\n")
        self.zip_path = self.tmp / "DevPack.zip"
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_reports_dirs_and_files(self):
        res = self.api.invoke("dev.pick", {"path": str(self.zip_path)})
        self.assertTrue(res["ok"], res)
        dev = res["data"]
        self.assertEqual(dev["phase"], "idle")
        names = {r["name"] for r in dev["strategies"]}
        self.assertIn("mods", names)
        self.assertEqual(dev["version"], "5.0.0")
        self.assertTrue(dev["options"]["include_hashes"])

    def test_export_produces_readable_pack(self):
        self.api.invoke("dev.pick", {"path": str(self.zip_path)})
        self.api.invoke("dev.changelog.set", {"text": "# 5.0.0\n\n- 导出测试\n"})
        self.api.invoke("dev.option.set", {"key": "min_size", "value": False})
        out = self.tmp / "out" / "DevPack_update.eapack"
        res = self.api.invoke("dev.export", {"path": str(out)})
        self.assertTrue(res["ok"], res)
        self.ctl.dev._thread.join(120)

        self.assertEqual(self.ctl.dev.phase, "done", self.ctl.dev.error)
        self.assertTrue(out.is_file())
        self.assertIn("导出测试", (eapack_mod.read_changelog(out) or ""))
        self.assertTrue(eapack_mod.read_hashes(out), "应当写入哈希清单")

    def test_option_and_exclusion_are_recorded(self):
        self.api.invoke("dev.pick", {"path": str(self.zip_path)})
        self.api.invoke("dev.option.set", {"key": "tamper_proof", "value": False})
        self.assertFalse(db.get_export_options()["tamper_proof"])
        res = self.api.invoke("dev.strategy.set",
                              {"name": "mods", "excluded": True})
        self.assertTrue(res["ok"])
        rows = {r["name"]: r for r in res["data"]["strategies"]}
        self.assertTrue(rows["mods"]["excluded"])


class SettingsAndResumeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-misc-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        db.set_db_path(dbdir)
        self.pack = self.tmp / "pack"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c = 1\n")
        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_whitelist_roundtrip_cleans_input(self):
        res = self.api.invoke("app.whitelist.set",
                              {"items": [" mods ", "mods", "config/", ""]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["whitelist"], ["mods", "config"])
        self.assertEqual(db.get_whitelist(), ["mods", "config"])
        got = self.api.invoke("app.whitelist.get")
        self.assertEqual(got["data"]["whitelist"], ["mods", "config"])

    def test_db_path_set_and_get(self):
        target = self.tmp / "moved" / "p.db"          # 前端传的是"文件"
        res = self.api.invoke("app.db.set", {"path": str(target)})
        self.assertTrue(res["ok"], res)
        self.assertEqual(Path(res["data"]["path"]), target.parent)
        self.assertEqual(Path(self.api.invoke("app.db.get")["data"]["path"]),
                         target.parent)

    def test_resume_info_after_scan(self):
        pack, update = self._build()
        self.api.invoke("app.pick_pack", {"path": str(pack)})
        self.api.invoke("update.pick", {"path": str(update)})
        self.api.invoke("pack.scan", {})
        self.ctl.flow._thread.join(60)
        self.assertIsNone(self.ctl.flow.error)

        # 模拟"下一次启动"：续传记录里有 1 个已完成文件
        self.ctl.flow.tasks = self.ctl.flow.collect_tasks()
        self.ctl.flow._done_paths = {"mods/a.jar"}
        self.ctl.flow._write_resume()
        info = self.ctl.flow.resume_info()
        self.assertIsNotNone(info)
        self.assertEqual(info["completed"], 1)

        res = self.api.invoke("update.resume", {})
        self.assertTrue(res["ok"], res)
        self.ctl.flow._thread.join(120)
        self.assertIsNone(self.ctl.flow.error)

    # ------------------------------------------------------------------
    def _build(self):
        from app.core.mrpack import merge_files, parse_mrpack
        src = self.tmp / "src"
        _write(src / "modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "versionId": "1.1",
            "name": "R", "files": [],
            "dependencies": {"minecraft": "1.20.1"}}))
        _write(src / "overrides" / "mods" / "a.jar", "A2")
        src_zip = self.tmp / "R.zip"
        with zipfile.ZipFile(src_zip, "w") as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())
        parsed, err = parse_mrpack(src_zip)
        assert parsed is not None, err
        update = self.tmp / "R.eapack"
        assert eapack_mod.export_eapack(
            source_dir=src, source_zip=src_zip, out_path=update,
            pack=parsed, merged_files=merge_files(parsed),
            strategies={}, changelog_md="# 1.1\n")
        return self.pack, update


if __name__ == "__main__":
    unittest.main()

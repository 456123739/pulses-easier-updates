"""开发者端 STEP 1 的唯一入口 dev.open：快判断 + 后台识别 + 角色路由。

要证明的三件事：
1. 拖/选完立刻返回（phase=loading），不把界面卡在点击上；
2. 识别完自己把状态推到位；晚到的旧结果不会覆盖新结果；
3. 认错东西（.eapack / 没有 index 的 zip）要说清原因，并且不动已有来源。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.core import instance_scan
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DevOpenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-devopen-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        # 实例：mods/ + config/ + 一个根散文件
        self.pack = self.tmp / "versions" / "DemoPack"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c = 1\n")
        _write(self.pack / "options.txt", "fov:90\n")

        self.pack2 = self.tmp / "versions" / "Second"
        _write(self.pack2 / "mods" / "b.jar", "B")
        _write(self.pack2 / "config" / "d.toml", "d = 2\n")

        # Modrinth 导出包
        self.zip_path = self.tmp / "DemoPack-1.0.0.mrpack"
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "name": "DemoPack",
                "versionId": "1.0.0", "files": [],
                "dependencies": {"minecraft": "1.20.1"}}))
            zf.writestr("overrides/mods/a.jar", "A")
            zf.writestr("overrides/config/c.toml", "c = 1\n")

        # 坏 index（能过 classify，但 parse_mrpack 会失败）
        self.bad_zip = self.tmp / "BadIndex.mrpack"
        with zipfile.ZipFile(self.bad_zip, "w") as zf:
            zf.writestr("modrinth.index.json", "{ not json")
            zf.writestr("overrides/mods/a.jar", "A")

        # 误拖的更新包
        self.eapack = self.tmp / "Demo_update.eapack"
        self.eapack.write_bytes(b"PK\x03\x04")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)

    # ---- 小工具 ----
    def dev(self) -> dict:
        res = self.api.invoke("dev.state", {})
        self.assertTrue(res["ok"], res)
        return res["data"]

    def wait_idle(self, timeout: float = 8.0) -> dict:
        end = time.time() + timeout
        snap = self.dev()
        while time.time() < end:
            snap = self.dev()
            if snap["phase"] != "loading":
                return snap
            time.sleep(0.02)
        self.fail(f"识别没有结束：{snap}")

    def open(self, path) -> dict:
        res = self.api.invoke("dev.open", {"path": str(path)})
        self.assertTrue(res["ok"], res)
        return res["data"]

    # ---- 主路径 ----
    def test_instance_returns_immediately_then_finishes(self):
        real = instance_scan.scan_instance

        def slow(*a, **kw):          # 让扫描慢下来，才能看出"没有同步等"
            time.sleep(0.35)
            return real(*a, **kw)

        with mock.patch.object(instance_scan, "scan_instance", slow):
            t0 = time.time()
            snap = self.open(self.pack)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 0.25, "dev.open 同步等了扫描")
        self.assertEqual(snap["phase"], "loading")
        self.assertEqual(snap["pending"]["kind"], "instance")
        self.assertEqual(snap["pending"]["name"], "DemoPack")
        self.assertTrue(self.ctl.dev.busy())

        done = self.wait_idle()
        self.assertEqual(done["phase"], "idle")
        self.assertIsNone(done["pending"])
        self.assertEqual(done["mode"], "instance")
        self.assertEqual(done["instance"]["name"], "DemoPack")
        self.assertEqual(sorted(s["name"] for s in done["strategies"]),
                         ["config", "mods", "options.txt"])

    def test_zip_source(self):
        self.open(self.zip_path)
        done = self.wait_idle()
        self.assertEqual(done["phase"], "idle")
        self.assertEqual(done["mode"], "zip")
        self.assertEqual(done["source"]["name"], "DemoPack-1.0.0.mrpack")
        self.assertEqual(sorted(s["name"] for s in done["strategies"]),
                         ["config", "mods"])

    def test_any_file_inside_instance_is_enough(self):
        """和侧边栏一样：选整合包里的任意文件也能定位。"""
        snap = self.open(self.pack / "mods" / "a.jar")
        self.assertEqual(snap["pending"]["name"], "DemoPack")
        done = self.wait_idle()
        self.assertEqual(done["mode"], "instance")

    # ---- 认错了要说清 ----
    def test_update_pack_rejected(self):
        res = self.api.invoke("dev.open", {"path": str(self.eapack)})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "IS_UPDATE_PACK")
        self.assertIn("更新包", res["error"]["message"])
        self.assertEqual(self.dev()["phase"], "idle")   # 没有进入识别

    def test_zip_without_index_rejected(self):
        path = self.tmp / "curseforge.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("overrides/mods/a.jar", "A")
        res = self.api.invoke("dev.open", {"path": str(path)})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "NOT_A_PACK")
        self.assertIn("modrinth.index.json", res["error"]["message"])

    def test_bad_index_keeps_previous_source(self):
        self.open(self.pack)
        self.assertEqual(self.wait_idle()["mode"], "instance")

        res = self.api.invoke("dev.open", {"path": str(self.bad_zip)})
        self.assertTrue(res["ok"], res)                  # 分类过了，识别时才失败
        done = self.wait_idle()
        self.assertEqual(done["phase"], "error")
        self.assertIn("modrinth.index.json", done["error"])
        # 之前的来源还在，不会被失败拖空
        self.assertEqual(done["mode"], "instance")
        self.assertEqual(done["instance"]["name"], "DemoPack")

    # ---- 连续拖入：后来居上 ----
    def test_second_open_wins(self):
        real = instance_scan.scan_instance

        def slow(*a, **kw):
            time.sleep(0.3)
            return real(*a, **kw)

        with mock.patch.object(instance_scan, "scan_instance", slow):
            self.open(self.pack)
            time.sleep(0.02)
            self.open(self.pack2)
        done = self.wait_idle()
        self.assertEqual(done["instance"]["name"], "Second",
                         "先开始的旧结果覆盖了后来的选择")

    # ---- 识别中不许导出 ----
    def test_export_blocked_while_loading(self):
        real = instance_scan.scan_instance
        with mock.patch.object(instance_scan, "scan_instance",
                               lambda *a, **k: (time.sleep(0.3), real(*a, **k))[1]):
            self.open(self.pack)
            res = self.api.invoke("dev.export", {"path": str(self.tmp / "out.eapack")})
        self.assertFalse(res["ok"])
        self.assertIn("识别", res["error"]["message"])

    # ---- 角色路由：同一个 STEP 1 ----
    def test_pick_pack_routes_by_role(self):
        # 玩家端：定位到整合包本体
        res = self.api.invoke("app.pick_pack", {"path": str(self.pack)})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["state"]["pack"]["name"], "DemoPack")

        # 开发者端：同一个调用进 dev.open（立即 loading，不等扫描）
        self.api.invoke("app.role.set", {"role": "developer"})
        real = instance_scan.scan_instance
        with mock.patch.object(instance_scan, "scan_instance",
                               lambda *a, **k: (time.sleep(0.3), real(*a, **k))[1]):
            res = self.api.invoke("app.pick_pack", {"path": str(self.pack2)})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["state"]["dev"]["phase"], "loading")
        done = self.wait_idle()
        self.assertEqual(done["instance"]["name"], "Second")

    def test_player_role_still_rejects_non_pack(self):
        res = self.api.invoke("app.pick_pack", {"path": str(self.eapack)})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "NOT_A_PACK")

    def test_drop_routes_to_dev_when_developer(self):
        self.api.invoke("app.role.set", {"role": "developer"})
        res = self.api.handle_drop([str(self.pack)])
        self.assertTrue(res["ok"], res)
        done = self.wait_idle()
        self.assertEqual(done["instance"]["name"], "DemoPack")

    def test_drop_wrong_file_reports_reason(self):
        self.api.invoke("app.role.set", {"role": "developer"})
        res = self.api.handle_drop([str(self.eapack)])
        self.assertFalse(res["ok"])
        self.assertIn("更新包", res["error"]["message"])


if __name__ == "__main__":
    unittest.main()

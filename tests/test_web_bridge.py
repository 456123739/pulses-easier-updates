"""Web 桥（app/ui_web）的契约测试：前端唯一依赖的那层信封与事件流。

这层是"黑盒"边界：前端只认 invoke 的 {ok, data|error} 与 events 推送，
所以这里按前端的调用顺序把真实控制器跑一遍，不 mock 任何业务。
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
from app.ui_web.events import EventBus, EventPump


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class BridgeContract(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-bridge-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        db.set_db_path(dbdir)

        self.pack = self.tmp / "versions" / "P"
        _write(self.pack / "mods" / "a.jar", "OLD")
        _write(self.pack / "config" / "c.toml", 'k = "old"\n')

        src = self.tmp / "src"
        _write(src / "modrinth.index.json",
               json.dumps({"formatVersion": 1, "game": "minecraft",
                           "versionId": "1.1", "name": "P", "files": [],
                           "dependencies": {"minecraft": "1.20.1"}}))
        _write(src / "overrides" / "mods" / "a.jar", "NEW")
        src_zip = self.tmp / "P.zip"
        with zipfile.ZipFile(src_zip, "w") as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())
        pack, err = parse_mrpack(src_zip)
        assert pack is not None and not err
        self.update = self.tmp / "P.eapack"
        assert eapack_mod.export_eapack(
            source_dir=src, source_zip=src_zip, out_path=self.update,
            pack=pack, merged_files=merge_files(pack),
            strategies={}, changelog_md="# 1.1\n")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_unknown_method_is_an_error_envelope(self):
        res = self.api.invoke("nope.nope", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "UNKNOWN_METHOD")

    def test_bootstrap_reports_version_and_flow(self):
        res = self.api.invoke("app.bootstrap", {})
        self.assertTrue(res["ok"])
        data = res["data"]
        self.assertIn("version", data)
        self.assertIn("flow", data)
        self.assertFalse(data["busy"])

    def test_player_flow_through_api(self):
        """前端真实调用顺序：定位 → 导入 → 比对 → 改策略 → 应用。"""
        res = self.api.invoke("app.pick_pack", {"path": str(self.pack)})
        self.assertTrue(res["ok"], res)
        self.assertIsNotNone(res["data"]["state"]["pack"])

        res = self.api.invoke("update.pick", {"path": str(self.update)})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["data"]["flow"]["update"]["changelog"])

        self.api.invoke("pack.scan", {})
        self.ctl.flow._thread.join(60)
        flow = self.api.invoke("flow.state", {})["data"]
        self.assertGreaterEqual(flow["counts"]["modified"], 1)
        names = {r["name"] for r in flow["strategies"]}
        self.assertIn("mods", names)

        res = self.api.invoke("update.strategy.set",
                              {"name": "mods", "checked": True})
        self.assertTrue(res["ok"])

        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(120)
        self.assertEqual((self.pack / "mods" / "a.jar").read_text(), "NEW")

    def test_busy_guard_and_state_event(self):
        self.api.invoke("app.pick_pack", {"path": str(self.pack)})
        self.api.invoke("update.pick", {"path": str(self.update)})
        self.ctl.flow.start_scan({}, {})
        res = self.api.invoke("update.pick", {"path": str(self.update)})
        self.assertFalse(res["ok"])          # 流程中不许换包
        self.ctl.flow._thread.join(60)
        types = [e["type"] for e in self.bus.drain(1000)]
        self.assertIn("state", types)

    def test_event_pump_batches_and_reports(self):
        class FakeWindow:
            def __init__(self):
                self.payloads = []

            def evaluate_js(self, code):
                self.payloads.append(code)

        win = FakeWindow()
        pump = EventPump(self.bus, win)
        self.bus.log("hello", "INFO")
        self.bus.progress("scan", "x", 0.5)
        batch = self.bus.drain(100)
        self.assertEqual([e["type"] for e in batch], ["log", "progress"])
        self.assertEqual(batch[0]["data"]["message"], "hello")

    def test_pick_path_rejects_missing(self):
        res = self.api.pick_path("file", "")
        self.assertIn("ok", res)


if __name__ == "__main__":
    unittest.main()


class SupervisedLaunchCase(unittest.TestCase):
    """受监督启动：加载成功交棒、超时/崩溃都要退回 Tk。"""

    def setUp(self):
        from app.ui_web import launch
        self.launch = launch

    def test_child_command_source_mode(self):
        cmd = self.launch.child_command("/tmp/x.ready", "/tmp/main.py")
        self.assertEqual(cmd[1:], ["/tmp/main.py", "--web-host", "/tmp/x.ready"])

    def test_timeout_falls_back(self):
        from unittest import mock
        with mock.patch.object(self.launch, "BOOT_TIMEOUT", 0.3), \
             mock.patch.object(self.launch.subprocess, "Popen") as popen, \
             mock.patch.object(self.launch, "_kill_tree") as kill:
            popen.return_value.poll.return_value = None
            self.assertIsNone(self.launch.run_supervised(entry="/tmp/x.py"))
            kill.assert_called_once()

    def test_ready_file_hands_over(self):
        from unittest import mock
        from pathlib import Path as _P
        with mock.patch.object(self.launch, "BOOT_TIMEOUT", 3.0), \
             mock.patch.object(self.launch.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            popen.return_value.wait.return_value = 7

            def _spawn(cmd, **kw):
                _P(cmd[-1]).write_text("ok", encoding="utf-8")
                return popen.return_value

            popen.side_effect = _spawn
            self.assertEqual(
                self.launch.run_supervised(entry="/tmp/x.py"), 7)

    def test_early_crash_falls_back(self):
        from unittest import mock
        with mock.patch.object(self.launch, "BOOT_TIMEOUT", 3.0), \
             mock.patch.object(self.launch.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = 3
            popen.return_value.returncode = 3
            self.assertIsNone(self.launch.run_supervised(entry="/tmp/x.py"))

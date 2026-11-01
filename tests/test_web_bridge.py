"""Web 桥（app/ui_web）的契约测试：前端唯一依赖的那层信封与事件流。

这层是"黑盒"边界：前端只认 invoke 的 {ok, data|error} 与 events 推送，
所以这里按前端的调用顺序把真实控制器跑一遍，不 mock 任何业务。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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


class E2eBridgeCase(unittest.TestCase):
    """端到端专用口子（e2e.config / e2e.fs / e2e.can_close / e2e_report）。

    它们只在 PULSES_E2E=1 时注册：产品里既没有这些方法，也读不到夹具目录
    以外的任何文件。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-e2e-api-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp / "work"
        self.pack = self.work / "versions" / "E2EPack"
        _write(self.pack / "mods" / "old.jar", "OLD")
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        db.set_db_path(dbdir)
        self.cfg = self.tmp / "cfg.json"
        self.cfg.write_text(json.dumps({
            "work": str(self.work),
            "suite": "all",
            "basic": {"pack": str(self.pack),
                      "update": str(self.work / "E2EPack-3.0.0.eapack")},
        }), encoding="utf-8")
        self.report = self.tmp / "report.json"
        self.bus = EventBus()
        self.ctl = AppController(self.bus)

    def _env(self, **extra):
        env = {"PULSES_E2E": "1",
               "PULSES_E2E_CFG": str(self.cfg),
               "PULSES_E2E_REPORT": str(self.report)}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def test_methods_are_absent_without_flag(self):
        api = Api(self.ctl, self.bus, e2e=False)
        res = api.invoke("e2e.config", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "UNKNOWN_METHOD")
        self.assertFalse(hasattr(api, "e2e_report"))

    def test_methods_are_registered_with_flag(self):
        with self._env():
            api = Api(self.ctl, self.bus)
            self.assertTrue(api.e2e)
            self.assertTrue(callable(getattr(api, "e2e_report", None)))
            res = api.invoke("e2e.config", {})
            self.assertTrue(res["ok"])
            data = res["data"]
            self.assertEqual(data["suite"], "all")
            # Windows 上临时目录可能带 8.3 短名（RUNNER~1），两边都 resolve 后再比
            self.assertEqual(Path(data["work"]).resolve(),
                             self.work.resolve())
            self.assertEqual(Path(data["basic"]["pack"]).resolve(),
                             self.pack.resolve())
            # 报告路径只走环境变量，配置里不带（免得两个来源打架）
            self.assertEqual(data["report"], str(self.report))

    def test_config_without_cfg_is_an_error(self):
        with mock.patch.dict(os.environ, {"PULSES_E2E": "1"}, clear=False):
            os.environ.pop("PULSES_E2E_CFG", None)
            os.environ.pop("PULSES_E2E_PACK", None)
            os.environ.pop("PULSES_E2E_UPDATE", None)
            api = Api(self.ctl, self.bus, e2e=True)
            res = api.invoke("e2e.config", {})
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], "NO_CONFIG")

    def test_fs_probe_reads_inside_and_rejects_escape(self):
        with self._env():
            api = Api(self.ctl, self.bus, e2e=True)
            jar = str(self.pack / "mods" / "old.jar")
            res = api.invoke("e2e.fs", {"paths": [jar], "text": True,
                                        "sha256": True})
            self.assertTrue(res["ok"])
            item = res["data"]["files"][jar]
            self.assertTrue(item["exists"])
            self.assertEqual(item["text"], "OLD")
            self.assertEqual(item["size"], 3)
            self.assertEqual(len(item["sha256"]), 64)
            # 目录之外的一律拒绝（防止探针变成任意文件读）
            outside = api.invoke("e2e.fs", {"paths": [str(self.cfg)]})
            self.assertFalse(outside["ok"])
            self.assertEqual(outside["error"]["code"], "FORBIDDEN")

    def test_can_close_mirrors_the_controller(self):
        with self._env():
            api = Api(self.ctl, self.bus, e2e=True)
            res = api.invoke("e2e.can_close", {})
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"], {"canClose": True, "blocked": False})

    def test_report_is_written_and_any_failed_step_fails_the_run(self):
        with self._env():
            api = Api(self.ctl, self.bus, e2e=True)
            bad = api.e2e_report({"steps": "不是数组"})
            self.assertFalse(bad["ok"])
            self.assertFalse(self.report.exists())

            res = api.e2e_report({
                "ok": True,
                "steps": [{"name": "a", "ok": True},
                          {"name": "b", "ok": False, "detail": "x"}],
                "artifacts": {"exported": "E"},
                "suite": "all",
            })
            self.assertTrue(res["ok"])
            self.assertTrue(self.report.is_file())
            data = json.loads(self.report.read_text(encoding="utf-8"))
            self.assertFalse(data["ok"])              # 有失败步 → 整体失败
            self.assertEqual([s["name"] for s in data["steps"]], ["a", "b"])
            self.assertEqual(data["artifacts"], {"exported": "E"})
            self.assertTrue(api.e2e_done)             # 关窗守卫据此放行
            self.assertFalse(self.report.with_name(
                self.report.name + ".tmp").exists())

    def test_report_ok_needs_steps(self):
        with self._env():
            api = Api(self.ctl, self.bus, e2e=True)
            api.e2e_report({"ok": True, "steps": []})
            data = json.loads(self.report.read_text(encoding="utf-8"))
            self.assertFalse(data["ok"])


class E2eHostUrlCase(unittest.TestCase):
    """入口 URL 的 `?e2e=1`：只有端到端才加，而且不能破坏已有的查询串。"""

    def test_adds_query_to_local_file(self):
        from app.ui_web.host import with_e2e_flag
        self.assertEqual(with_e2e_flag("C:/x/web/dist/index.html"),
                         "C:/x/web/dist/index.html?e2e=1")

    def test_keeps_existing_query(self):
        from app.ui_web.host import with_e2e_flag
        self.assertEqual(with_e2e_flag("http://127.0.0.1:5273/?a=1"),
                         "http://127.0.0.1:5273/?a=1&e2e=1")

    def test_dev_target_with_query(self):
        from app.ui_web.host import with_e2e_flag
        self.assertEqual(with_e2e_flag("http://127.0.0.1:5273/index.html?x=2"),
                         "http://127.0.0.1:5273/index.html?x=2&e2e=1")

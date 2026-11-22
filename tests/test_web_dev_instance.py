"""开发者端：从本地实例导出（instance 模式）的流程测试。"""

import json
import tempfile
import unittest
import zipfile

import packread
from pathlib import Path
from unittest import mock

from app.ui_web import dev as dev_mod


class FakeBus:
    def __init__(self):
        self.logs = []
        self.progress_calls = []

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def progress(self, kind, label, value):
        self.progress_calls.append((kind, label, value))

    def emit(self, *args, **kwargs):
        pass


class FakeController:
    def __init__(self):
        self.pushes = 0

    def push_state(self):
        self.pushes += 1


def build_instance(root: Path) -> None:
    (root / "mods").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "screenshots").mkdir()
    (root / "mods" / "a.jar").write_bytes(b"A" * 1024)
    (root / "config" / "x.toml").write_text("k=1\n", encoding="utf-8")
    (root / "screenshots" / "s.png").write_bytes(b"S" * 10)


class InstanceModeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-devi-"))
        self.root = self.tmp / "Pack"
        self.root.mkdir()
        build_instance(self.root)
        self.ctrl = FakeController()
        self.bus = FakeBus()
        self.flow = dev_mod.DevFlow(self.ctrl, self.bus)
        self.flow.import_instance(str(self.root))

    def test_snapshot_shape(self):
        snap = self.flow.snapshot()
        self.assertEqual(snap["mode"], "instance")
        inst = snap["instance"]
        self.assertEqual(inst["name"], "Pack")
        self.assertEqual(inst["totals"]["files"], 2)
        self.assertEqual({r["name"] for r in inst["rows"]}, {"mods", "config"})
        self.assertEqual(snap["strategies"][0]["name"], "config")
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertIn("智能剔除", rows["config"]["strategies"])
        self.assertEqual(rows["config"]["current"], "智能剔除")

        self.assertTrue(snap["ruleOptions"])
        self.assertIn("hasCfKey", snap)

    def test_missing_root_file_can_be_selected(self):
        (self.root / "servers.dat").write_bytes(b"D" * 16)
        self.flow._rescan()
        rows = {r["name"]: r for r in self.flow.snapshot()["strategies"]}
        self.assertIn("servers.dat", rows)
        self.assertTrue(rows["servers.dat"]["excluded"])
        snap = self.flow.set_scope(["mods", "servers.dat"])
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertFalse(rows["servers.dat"]["excluded"])

    def test_set_scope_only_keeps_selected_and_estimates(self):
        snap = self.flow.set_scope(["config"])
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertTrue(rows["config"]["excluded"] is False)
        self.assertTrue(rows["mods"]["excluded"] is True)
        info = self.flow.estimate()
        self.assertGreater(info["files"], 0)
        self.assertLessEqual(info["pack_low"], info["pack_high"])

    def test_scan_respects_default_rules(self):
        rows = {r["name"] for r in self.flow.snapshot()["instance"]["rows"]}
        self.assertNotIn("screenshots", rows)

    def test_never_pack_files_not_in_scope(self):
        (self.root / "options.txt").write_text("fov:0.5\n", encoding="utf-8")
        self.flow._rescan()
        names = list(self.flow.tops) + list(self.flow.root_files)
        self.assertNotIn("options.txt", names)

    def test_set_rule_rescans(self):
        self.flow.set_rule("screenshots", True)
        rows = {r["name"] for r in self.flow.snapshot()["strategies"]}
        self.assertIn("screenshots", rows)

    def test_set_rule_rejects_unknown(self):
        with self.assertRaises(dev_mod.DevError):
            self.flow.set_rule("nope", True)

    def test_import_requires_modpack_dir(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(dev_mod.DevError):
            self.flow.import_instance(str(empty))
        with self.assertRaises(dev_mod.DevError):
            self.flow.import_instance(str(self.tmp / "missing"))

    def test_export_produces_pack(self):
        out = self.tmp / "out" / "pack.eapack"
        with mock.patch.object(dev_mod.inst_export.lookup, "resolve",
                               return_value={}):
            self.flow._worker_instance(out)
        self.assertEqual(self.flow.phase, "done")
        self.assertTrue(out.is_file())
        names = set(packread.names(out))
        index = json.loads(packread.read(out, "modrinth.index.json"))
        self.assertIn("overrides/mods/a.jar", names)
        self.assertEqual(index["files"], [])
        self.assertEqual(index["name"], "Pack")

    def test_export_uses_strategies_and_skips_excluded(self):
        self.flow.set_strategy("config", "智能剔除")
        self.flow.set_strategy("mods", excluded=True)
        out = self.tmp / "out2.eapack"
        with mock.patch.object(dev_mod.inst_export.lookup, "resolve",
                               return_value={}):
            self.flow._worker_instance(out)
        settings = json.loads(packread.read(out, "ea_settings.json"))
        names = set(packread.names(out))
        self.assertEqual(settings["recommended_strategies"], {"config": "智能剔除"})
        self.assertFalse(any(n.startswith("overrides/mods/") for n in names))

    def test_excluded_root_file_is_not_packed(self):
        (self.root / "servers.dat").write_bytes(b"D" * 32)
        self.flow.set_rule("servers", True)
        self.flow.set_strategy("servers.dat", excluded=True)
        out = self.tmp / "out3.eapack"
        with mock.patch.object(dev_mod.inst_export.lookup, "resolve",
                               return_value={}):
            self.flow._worker_instance(out)
        names = set(packread.names(out))
        self.assertNotIn("overrides/servers.dat", names)

    def test_rules_change_adds_new_rows_to_scope(self):
        # 现在完整展示所有顶层条目；默认不勾选的以 excluded 标记
        rows = {r["name"]: r for r in self.flow.snapshot()["strategies"]}
        self.assertIn("screenshots", rows)
        self.flow.set_rule("screenshots", True)
        rows = {r["name"]: r for r in self.flow.snapshot()["strategies"]}
        self.assertFalse(rows["screenshots"]["excluded"])

    def test_double_export_rejected(self):
        import threading as _th
        gate = _th.Event()

        def slow(*args, **kwargs):
            gate.wait(3)
            return None

        out = self.tmp / "double.eapack"
        with mock.patch.object(dev_mod.inst_export, "export_from_instance",
                               side_effect=slow):
            self.flow.start_export(str(out))
            with self.assertRaises(dev_mod.DevError):
                self.flow.start_export(str(out))
            gate.set()
            self.flow._thread.join(5)

    def test_export_pushes_exporting_state(self):
        import threading as _th
        gate = _th.Event()

        def slow(*args, **kwargs):
            gate.wait(3)
            return None

        before = self.ctrl.pushes
        with mock.patch.object(dev_mod.inst_export, "export_from_instance",
                               side_effect=slow):
            self.flow.start_export(str(self.tmp / "p.eapack"))
            self.assertEqual(self.flow.phase, "exporting")
            self.assertGreater(self.ctrl.pushes, before)
            gate.set()
            self.flow._thread.join(5)

    def test_abort_cancels_export(self):
        import threading as _th
        started = _th.Event()
        gate = _th.Event()

        def slow(*args, cancel=None, **kwargs):
            started.set()
            gate.wait(3)
            if cancel is not None and cancel.is_set():
                return None
            return None

        out = self.tmp / "abort.eapack"
        with mock.patch.object(dev_mod.inst_export, "export_from_instance",
                               side_effect=slow):
            self.flow.start_export(str(out))
            started.wait(3)
            self.flow.abort()
            self.assertTrue(self.flow._cancel.is_set())
            gate.set()
            self.flow._thread.join(5)

    def test_cancel_marks_idle_not_error(self):
        import threading as _th
        started = _th.Event()
        gate = _th.Event()

        def slow(*args, cancel=None, **kwargs):
            started.set()
            gate.wait(3)
            cancel.set()
            return None

        out = self.tmp / "cancel.eapack"
        with mock.patch.object(dev_mod.inst_export, "export_from_instance",
                               side_effect=slow):
            self.flow.start_export(str(out))
            started.wait(3)
            gate.set()
            self.flow._thread.join(5)
        self.assertEqual(self.flow.phase, "idle")
        self.assertIsNone(self.flow.error)

    def test_clear_resets_mode(self):
        self.flow.clear()
        snap = self.flow.snapshot()
        self.assertEqual(snap["mode"], "zip")
        self.assertIsNone(snap["instance"])


if __name__ == "__main__":
    unittest.main()

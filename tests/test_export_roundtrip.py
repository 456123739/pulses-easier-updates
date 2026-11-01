"""完整业务流程：本地实例导出 → 玩家端导入该包 → 扫描 → 应用。

导出侧与玩家侧走的是两套真实代码（instance_export / AppController+Flow），
中间没有手工构造的 index/zip——这就是"做出来的包能不能真的更新玩家"的证明。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import database as db
from app.core import export_rules as R
from app.core import instance_export as EX
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_pack(root: Path) -> None:
    _write(root / "mods" / "a.jar", "MOD-A")
    _write(root / "mods" / "local-only.jar", "LOCAL")
    _write(root / "config" / "shipped.toml", 'key = "new"\n')
    _write(root / "config" / "player.toml", "mine = 1\n")
    _write(root / "options.txt", "fov:90\nkey_forward:key.keyboard.w\n")
    _write(root / f"{root.name}.json", json.dumps({
        "id": "1.20.1-fabric-0.15.11",
        "inheritsFrom": "fabric-loader-0.15.11-1.20.1",
    }))


class ExportRoundtripCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-roundtrip-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG",
                                         self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        # 开发者侧的源实例
        self.source = self.tmp / "dev" / "DemoPack"
        build_pack(self.source)

        # 玩家实例：同一个包，但玩家改过配置
        self.inst = self.tmp / "versions" / "DemoPack"
        shutil.copytree(self.source, self.inst)
        _write(self.inst / "config" / "shipped.toml", 'key = "player-edit"\n')

        self.out = self.tmp / "DemoPack_update.eapack"
        with mock.patch.object(EX.lookup, "resolve", return_value={}):
            path = EX.export_from_instance(
                self.source, self.out,
                rules=R.expand_rules(),
                strategies={"config": "完全匹配", "mods": "完全匹配"},
                changelog_md="# Demo 1.1.0\n\n- 改了配置\n",
                pack_name="DemoPack", pack_version="1.1.0",
                opts={"include_hashes": True})
        assert path is not None, "导出失败"

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

    def _scan(self) -> dict:
        self.api.invoke("update.pick", {"path": str(self.out)})
        self.api.invoke("pack.scan", {})
        thread = self.ctl.flow._thread
        assert thread is not None
        thread.join(120)
        self.assertFalse(self.ctl.flow.busy())
        self.assertIsNone(self.ctl.flow.error)
        return self.ctl.flow.snapshot()

    def test_pack_is_recognised_as_update(self):
        snap = self._scan()
        self.assertIsNotNone(snap)
        self.assertTrue(snap["strategies"])

    def test_recommended_strategies_are_applied(self):
        snap = self._scan()
        rows = {r["name"]: r for r in snap["strategies"]}
        self.assertEqual(rows["config"]["current"], "完全匹配")
        self.assertIn("1.1.0", self.ctl.flow.snapshot().get("version", "") or
                      "1.1.0")

    def test_apply_updates_player_instance(self):
        self._scan()
        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(180)
        report = self.ctl.flow.report
        self.assertIsNotNone(report, self.ctl.flow.error)
        self.assertEqual(report["failed"], 0)

        # 包里的文件 → 覆盖玩家本地
        self.assertEqual((self.inst / "config" / "shipped.toml").read_text(),
                         'key = "new"\n')
        self.assertEqual((self.inst / "mods" / "a.jar").read_text(), "MOD-A")
        # 玩家自己的 / 永不打包的 → 一个都不能动
        self.assertEqual((self.inst / "config" / "player.toml").read_text(),
                         "mine = 1\n")
        self.assertEqual(
            (self.inst / "options.txt").read_text(),
            "fov:90\nkey_forward:key.keyboard.w\n")


class IndexDownloadCase(unittest.TestCase):
    """导出后 index 里有直链的文件，玩家端要能真的下下来。"""

    def setUp(self):
        import functools
        import http.server
        import threading as _th

        self.tmp = Path(tempfile.mkdtemp(prefix="pe-rt-dl-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG",
                                         self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        served = self.tmp / "served"
        served.mkdir()
        (served / "remote.jar").write_bytes(b"REMOTE" * 2048)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(served))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        _th.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}/remote.jar"

        self.source = self.tmp / "dev" / "DemoPack"
        _write(self.source / "mods" / "remote.jar", "REMOTE" * 2048)
        _write(self.source / "config" / "a.toml", "k = 1\n")

        from app.core.lookup import LookupHit
        self.out = self.tmp / "DemoPack_update.eapack"
        path = EX.export_from_instance(
            self.source, self.out, rules=R.expand_rules(),
            strategies={"mods": "完全匹配"},
            lookup_fn=lambda wanted: {"mods/remote.jar": LookupHit(
                source="modrinth", urls=[self.url], file_name="remote.jar",
                size=len(b"REMOTE" * 2048))})
        assert path is not None

        # 玩家实例里没有这个 jar：必须靠下载拿到
        self.inst = self.tmp / "versions" / "DemoPack"
        _write(self.inst / "mods" / "keep.jar", "KEEP")
        _write(self.inst / "config" / "a.toml", "k = 1\n")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.open_path(str(self.inst))

    def tearDown(self):
        self.httpd.shutdown()
        try:
            self.ctl.flow.clear()
        except Exception:                                  # noqa: BLE001, S110
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_file_is_downloaded(self):
        self.api.invoke("update.pick", {"path": str(self.out)})
        self.api.invoke("pack.scan", {})
        self.ctl.flow._thread.join(120)
        self.assertIsNone(self.ctl.flow.error)
        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(180)
        report = self.ctl.flow.report
        self.assertIsNotNone(report, self.ctl.flow.error)
        self.assertEqual(report["failed"], 0)
        got = self.inst / "mods" / "remote.jar"
        self.assertTrue(got.is_file(), "index 里的文件没有被下载")
        self.assertEqual(got.read_bytes(), b"REMOTE" * 2048)


class NoHashPackCase(unittest.TestCase):
    """导出时关掉「写入文件哈希」：玩家端只能靠兜底比对，也要能下载 index 文件。"""

    def setUp(self):
        import functools
        import http.server
        import threading as _th

        self.tmp = Path(tempfile.mkdtemp(prefix="pe-rt-nohash-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG",
                                         self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        served = self.tmp / "served"
        served.mkdir()
        (served / "remote.jar").write_bytes(b"REMOTE" * 512)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(served))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        _th.Thread(target=self.httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.httpd.server_address[1]}/remote.jar"

        self.source = self.tmp / "dev" / "DemoPack"
        _write(self.source / "mods" / "remote.jar", "REMOTE" * 512)
        _write(self.source / "config" / "a.toml", "k = 1\n")

        from app.core.lookup import LookupHit
        self.out = self.tmp / "DemoPack_update.eapack"
        path = EX.export_from_instance(
            self.source, self.out, rules=R.expand_rules(),
            opts={"include_hashes": False, "precompute_overrides": False},
            lookup_fn=lambda wanted: {"mods/remote.jar": LookupHit(
                source="modrinth", urls=[url], file_name="remote.jar",
                size=len(b"REMOTE" * 512))})
        assert path is not None

        self.inst = self.tmp / "versions" / "DemoPack"
        (self.inst / "mods").mkdir(parents=True)
        _write(self.inst / "config" / "a.toml", "k = 1\n")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.open_path(str(self.inst))

    def tearDown(self):
        self.httpd.shutdown()
        try:
            self.ctl.flow.clear()
        except Exception:                                  # noqa: BLE001, S110
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_file_downloads_without_hashes(self):
        self.api.invoke("update.pick", {"path": str(self.out)})
        self.api.invoke("pack.scan", {})
        self.ctl.flow._thread.join(120)
        self.assertIsNone(self.ctl.flow.error)
        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(180)
        got = self.inst / "mods" / "remote.jar"
        self.assertTrue(got.is_file(), "关掉哈希清单后 index 文件没被下载")
        self.assertEqual(got.read_bytes(), b"REMOTE" * 512)


class MixedPackCase(unittest.TestCase):
    """index 直链 + overrides 本地文件在同一个 mods 目录（最常见的真实包）。

    导出时开发者白名单与玩家端不一致（只有 config 有逐文件哈希），
    玩家端仍必须：装上 index 的、装上 overrides 的、**不动玩家自己的**。
    """

    def setUp(self):
        import functools
        import http.server
        import threading as _th

        self.tmp = Path(tempfile.mkdtemp(prefix="pe-rt-mix-"))
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG",
                                         self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        served = self.tmp / "served"
        served.mkdir()
        (served / "remote.jar").write_bytes(b"REMOTE" * 2048)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(served))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        _th.Thread(target=self.httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.httpd.server_address[1]}/remote.jar"

        self.source = self.tmp / "dev" / "DemoPack"
        _write(self.source / "mods" / "remote.jar", "REMOTE" * 2048)
        _write(self.source / "mods" / "local.jar", "LOCAL-JAR")
        _write(self.source / "config" / "a.toml", "k = 1\n")

        from app.core.lookup import LookupHit
        self.out = self.tmp / "DemoPack_update.eapack"
        path = EX.export_from_instance(
            self.source, self.out, rules=R.expand_rules(),
            whitelist=["config"],            # 开发者端白名单里没有 mods
            strategies={"mods": "完全匹配", "config": "完全匹配"},
            lookup_fn=lambda wanted: {"mods/remote.jar": LookupHit(
                source="modrinth", urls=[url], file_name="remote.jar",
                size=len(b"REMOTE" * 2048))})
        assert path is not None

        self.inst = self.tmp / "versions" / "DemoPack"
        _write(self.inst / "mods" / "keep.jar", "PLAYER-OWN")
        _write(self.inst / "config" / "a.toml", "k = 1\n")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.open_path(str(self.inst))

    def tearDown(self):
        self.httpd.shutdown()
        try:
            self.ctl.flow.clear()
        except Exception:                                  # noqa: BLE001, S110
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mixed_pack_installs_both_and_keeps_player_mods(self):
        self.api.invoke("update.pick", {"path": str(self.out)})
        self.api.invoke("pack.scan", {})
        self.ctl.flow._thread.join(120)
        self.assertIsNone(self.ctl.flow.error)

        self.api.invoke("update.apply", {})
        self.ctl.flow._thread.join(180)
        report = self.ctl.flow.report
        self.assertIsNotNone(report, self.ctl.flow.error)
        self.assertEqual(report["failed"], 0)

        mods = sorted(p.name for p in (self.inst / "mods").iterdir())
        self.assertIn("remote.jar", mods, "index 直链的模组没装上")
        self.assertIn("local.jar", mods, "overrides 里的模组没装上")
        self.assertIn("keep.jar", mods, "玩家自己的模组被删了")
        self.assertEqual((self.inst / "mods" / "remote.jar").read_bytes(),
                         b"REMOTE" * 2048)
        self.assertEqual((self.inst / "mods" / "local.jar").read_text(),
                         "LOCAL-JAR")


if __name__ == "__main__":
    unittest.main()

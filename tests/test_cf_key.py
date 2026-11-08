"""CurseForge API Key：默认用 PCL 内置那把，用户填的优先，环境变量可覆盖/禁用。"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core import cf_key, database as db, instance_export, lookup
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ResolveCase(unittest.TestCase):
    def test_builtin_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cf_key.ENV_NAME, None)
            self.assertEqual(cf_key.resolve(""), cf_key.DEFAULT_CF_API_KEY)
            self.assertEqual(cf_key.resolve(None), cf_key.DEFAULT_CF_API_KEY)
            self.assertEqual(cf_key.source(""), cf_key.SOURCE_BUILTIN)

    def test_user_key_wins(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cf_key.ENV_NAME, None)
            self.assertEqual(cf_key.resolve("my-key"), "my-key")
            self.assertEqual(cf_key.source("my-key"), cf_key.SOURCE_USER)

    def test_env_overrides_everything(self):
        with mock.patch.dict(os.environ, {cf_key.ENV_NAME: "env-key"}):
            self.assertEqual(cf_key.resolve("my-key"), "env-key")
            self.assertEqual(cf_key.source("my-key"), cf_key.SOURCE_USER)

    def test_env_empty_disables_curseforge(self):
        """测试/CI 用 PULSES_CF_KEY="" 关掉 CF 查询，保持离线可重复。"""
        with mock.patch.dict(os.environ, {cf_key.ENV_NAME: ""}):
            self.assertEqual(cf_key.resolve("my-key"), "")
            self.assertEqual(cf_key.source("my-key"), cf_key.SOURCE_OFF)

    def test_builtin_looks_like_a_key(self):
        self.assertTrue(cf_key.DEFAULT_CF_API_KEY.startswith("$2a$10$"))
        self.assertGreater(len(cf_key.DEFAULT_CF_API_KEY), 50)


class AuthCase(unittest.TestCase):
    def test_401_is_auth_failure(self):
        def boom(*_a, **_k):
            raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                "https://api.curseforge.com/v1/fingerprints/432", 401, "no", {}, None)

        client = lookup.CurseForgeClient("bad", fetch=boom, retries=0,
                                         sleep=lambda _s: None)
        with self.assertRaises(lookup.LookupDenied):
            client.match([123])

    def test_no_key_is_denied(self):
        with self.assertRaises(lookup.LookupDenied):
            lookup.CurseForgeClient("").match([123])


class ExportUsesResolvedKeyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-cfkey-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        self.pack = self.tmp / "Pack"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.set_role("developer")

    def load(self) -> dict:
        self.api.invoke("dev.open", {"path": str(self.pack)})
        for _ in range(300):
            snap = self.api.invoke("dev.state", {})["data"]
            if snap["phase"] != "loading":
                return snap
            time.sleep(0.02)
        self.fail("识别没结束")

    def test_snapshot_and_export_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cf_key.ENV_NAME, None)
            snap = self.load()
            self.assertEqual(snap["cfKeySource"], cf_key.SOURCE_BUILTIN)
            self.assertFalse(snap["hasCfKey"])

            seen: dict = {}

            def fake_export(root, out, **kw):
                seen.update(kw)
                out.write_bytes(b"PK\x03\x04" + b"0" * 600)
                return {"out": str(out)}

            with mock.patch.object(instance_export, "export_from_instance", fake_export):
                self.api.invoke("dev.export", {"path": str(self.tmp / "o.eapack")})
                for _ in range(300):
                    if seen:
                        break
                    time.sleep(0.02)
            self.assertEqual(seen.get("cf_key"), cf_key.DEFAULT_CF_API_KEY)

            # 用户填自己的 → 用用户的；清空 → 回到内置
            snap = self.api.invoke("dev.cfkey.set", {"key": "user-key"})["data"]
            self.assertEqual(snap["cfKeySource"], cf_key.SOURCE_USER)
            with mock.patch.object(instance_export, "export_from_instance", fake_export):
                seen.clear()
                self.api.invoke("dev.export", {"path": str(self.tmp / "o2.eapack")})
                for _ in range(300):
                    if seen:
                        break
                    time.sleep(0.02)
            self.assertEqual(seen.get("cf_key"), "user-key")

            snap = self.api.invoke("dev.cfkey.set", {"key": ""})["data"]
            self.assertEqual(snap["cfKeySource"], cf_key.SOURCE_BUILTIN)


if __name__ == "__main__":
    unittest.main()

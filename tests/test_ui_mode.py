"""界面选择（旧界面 / 新界面）的读写与默认值。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.core import database


class UiModeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.json"
        self._patch = mock.patch.object(database, "_APP_CFG", self.cfg)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_default_is_auto(self):
        self.assertEqual(database.get_ui_mode(), "auto")

    def test_roundtrip(self):
        database.set_ui_mode("web")
        self.assertEqual(database.get_ui_mode(), "web")
        database.set_ui_mode("tk")
        self.assertEqual(database.get_ui_mode(), "tk")

    def test_unknown_value_falls_back_to_default(self):
        database.set_ui_mode("nonsense")
        self.assertEqual(database.get_ui_mode(), "auto")

    def test_reading_broken_config_is_safe(self):
        self.cfg.write_text("{not json", encoding="utf-8")
        self.assertEqual(database.get_ui_mode(), "auto")

    def test_setting_keeps_other_keys(self):
        database.save_app_config({"db_path": "/tmp/db"})
        database.set_ui_mode("web")
        self.assertEqual(database.load_app_config()["db_path"], "/tmp/db")

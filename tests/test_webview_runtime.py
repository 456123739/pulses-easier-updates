"""WebView2 运行时检测：用户级安装（%LOCALAPPDATA%）不能漏。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.ui_web import host as host_mod
from app.ui_web import launch as launch_mod


class RuntimeAvailableCase(unittest.TestCase):
    def test_localappdata_install_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = (Path(tmp) / "Microsoft" / "EdgeWebView" / "Application"
                   / "120.0.1" / "msedgewebview2.exe")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"x")
            env = {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(launch_mod.sys, "platform", "win32"), \
                    mock.patch.object(host_mod.sys, "platform", "win32"):
                self.assertTrue(launch_mod.runtime_available())
                self.assertTrue(host_mod.runtime_available())

    def test_no_runtime_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(launch_mod.sys, "platform", "win32"), \
                    mock.patch.object(host_mod.sys, "platform", "win32"):
                self.assertFalse(launch_mod.runtime_available())
                self.assertFalse(host_mod.runtime_available())


if __name__ == "__main__":
    unittest.main()

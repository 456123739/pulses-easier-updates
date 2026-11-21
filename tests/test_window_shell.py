"""无边框窗口：logo 小窗展开成主窗口的几何计算 + window.* 桥。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.ui_web import window_shell as ws
from app.ui_web.api import Api
from app.ui_web.events import EventBus


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.maximized = False
        self.min_size = None

    def resize(self, w, h):
        self.calls.append(("resize", w, h))

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def minimize(self):
        self.calls.append(("minimize",))

    def maximize(self):
        self.maximized = True
        self.calls.append(("maximize",))

    def restore(self):
        self.maximized = False
        self.calls.append(("restore",))

    def destroy(self):
        self.calls.append(("destroy",))


class WindowShellCase(unittest.TestCase):
    def test_center_rect(self):
        self.assertEqual(ws.center_rect(300, 300, (1440, 900)), (570, 200))

    def test_expand_animates_and_clamps_to_screen(self):
        win = FakeWindow()
        out = ws.expand(win, (1440, 860), duration=0, steps=8,
                        screen=(1280, 720), sleep=lambda s: None)
        self.assertEqual(out, (1200, 620))          # 1280-80 / 720-100
        self.assertEqual(len([c for c in win.calls if c[0] == "resize"]), 8)
        last = [c for c in win.calls if c[0] == "resize"][-1]
        self.assertEqual(last[1:], (1200, 620))
        self.assertEqual(win.min_size, ws.MIN_SIZE)

    def test_compute_resize_keeps_opposite_edge(self):
        start = {"x": 100, "y": 100, "width": 1000, "height": 700}
        got = ws.compute_resize("nw", start, 40, 20)
        self.assertEqual(got["x"], 140)
        self.assertEqual(got["y"], 120)
        self.assertEqual(got["width"], 960)
        self.assertEqual(got["height"], 680)
        # 拖到比最小尺寸还小：夹住，左上边跟着动
        got = ws.compute_resize("nw", start, 900, 900)
        self.assertEqual(got["width"], ws.MIN_SIZE[0])
        self.assertEqual(got["x"], 100 + 1000 - ws.MIN_SIZE[0])


class WindowApiCase(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.ctl = mock.Mock()
        self.ctl.can_close.return_value = True
        self.api = Api(self.ctl, self.bus, e2e=False)
        self.win = FakeWindow()

    def test_no_window_in_browser(self):
        res = self.api.invoke("window.minimize", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "NO_WINDOW")

    def test_controls(self):
        self.api.bind_window(self.win)
        self.assertTrue(self.api.invoke("window.minimize", {})["ok"])
        res = self.api.invoke("window.toggle_max", {})
        self.assertTrue(res["data"]["maximized"])
        res = self.api.invoke("window.toggle_max", {})
        self.assertFalse(res["data"]["maximized"])
        res = self.api.invoke("window.resize", {"width": 1200, "height": 800})
        self.assertEqual(res["data"], {"width": 1200, "height": 800})
        self.assertIn(("resize", 1200, 800), self.win.calls)

    def test_close_respects_guard(self):
        self.api.bind_window(self.win)
        self.ctl.can_close.return_value = False
        res = self.api.invoke("window.close", {})
        self.assertEqual(res["data"], {"closed": False, "blocked": True})
        self.assertNotIn(("destroy",), self.win.calls)
        self.ctl.can_close.return_value = True
        res = self.api.invoke("window.close", {})
        self.assertTrue(res["data"]["closed"])
        self.assertIn(("destroy",), self.win.calls)


if __name__ == "__main__":
    unittest.main()

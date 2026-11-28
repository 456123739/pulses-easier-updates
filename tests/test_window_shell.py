"""无边框窗口：logo 小窗展开成主窗口的几何计算 + window.* 桥。"""

from __future__ import annotations

import threading
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


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


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

    def test_native_resize_keeps_region_current_and_includes_work_in_budget(self):
        clock = FakeClock()
        frames = []
        regions = []

        def resize(hwnd, x, y, w, h):
            frames.append((clock(), w, h))
            # Each frame must already expose the newly sized client area.
            self.assertEqual(regions[-1], ((w, h), False))
            clock.now += 0.006
            return True

        with mock.patch.object(ws.sys, "platform", "win32"), \
                mock.patch.object(ws, "find_hwnd", return_value=101), \
                mock.patch.object(ws, "_set_window_rect", side_effect=resize), \
                mock.patch.object(ws, "_set_region", side_effect=lambda h, r, **kw:
                                  regions.append((kw["size"], kw["redraw"]))), \
                mock.patch.object(ws, "apply_window_rounding") as final_rounding:
            win = FakeWindow()
            out = ws.expand(win, (1440, 860), screen=(1920, 1080),
                            sleep=clock.sleep, clock=clock)

        self.assertEqual(out, (1440, 860))
        self.assertEqual(len(frames), 34)  # 560ms at a nominal 60Hz
        self.assertEqual(len(regions), len(frames))
        self.assertEqual(frames[-1][1:], out)
        self.assertAlmostEqual(clock.now, 0.566)
        gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
        self.assertLess(max(gaps), 1 / 59)
        final_rounding.assert_called_once_with(win, ws.RADIUS_PX)

    def test_slow_native_resize_skips_stale_frames_and_still_reaches_target(self):
        clock = FakeClock()
        frames = []

        def resize(hwnd, x, y, w, h):
            frames.append((clock(), w, h))
            clock.now += 0.04  # A stalled native UI thread misses more than one tick.
            return True

        with mock.patch.object(ws.sys, "platform", "win32"), \
                mock.patch.object(ws, "find_hwnd", return_value=101), \
                mock.patch.object(ws, "_set_window_rect", side_effect=resize), \
                mock.patch.object(ws, "_set_region"), \
                mock.patch.object(ws, "apply_window_rounding"):
            ws.expand(FakeWindow(), (1440, 860), screen=(1920, 1080),
                      sleep=clock.sleep, clock=clock)

        self.assertLess(len(frames), 20)
        self.assertEqual(frames[-1][1:], (1440, 860))
        self.assertLess(clock.now, 0.56 + 0.04 + 1 / 60)
        self.assertTrue(all(a[1] <= b[1] for a, b in zip(frames, frames[1:])))

    def test_reduced_motion_sets_final_size_once(self):
        win = FakeWindow()
        out = ws.expand(win, (1440, 860), duration=0, screen=(1280, 720),
                        sleep=lambda s: self.fail("reduced motion must not wait"))
        self.assertEqual(out, (1200, 620))
        self.assertEqual([c for c in win.calls if c[0] == "resize"],
                         [("resize", 1200, 620)])
        self.assertEqual(win.min_size, ws.MIN_SIZE)

    def test_region_uses_known_frame_size_without_native_rect_query(self):
        user32, gdi32 = mock.Mock(), mock.Mock()
        gdi32.CreateRoundRectRgn.return_value = 301
        user32.SetWindowRgn.return_value = 1
        with mock.patch.object(ws, "_win32", return_value=(user32, gdi32, None, None)), \
                mock.patch.object(ws, "_window_rect") as rect:
            self.assertTrue(ws._set_region(101, size=(900, 560), redraw=False))
        rect.assert_not_called()
        gdi32.CreateRoundRectRgn.assert_called_once_with(0, 0, 901, 561, 24, 24)
        user32.SetWindowRgn.assert_called_once_with(101, 301, False)
        gdi32.DeleteObject.assert_not_called()  # SetWindowRgn takes ownership.

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

    def test_expand_response_waits_for_native_completion_and_is_idempotent(self):
        self.api.bind_window(self.win)
        entered, complete = threading.Event(), threading.Event()
        responses = []

        def expanding(win, target):
            entered.set()
            self.assertTrue(complete.wait(2))
            return (1200, 620)

        with mock.patch.object(ws, "expand", side_effect=expanding) as expand:
            worker = threading.Thread(
                target=lambda: responses.append(self.api.invoke("window.expand", {})))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(responses, [])
            finally:
                complete.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(responses[0]["data"],
                             {"expanding": False, "target": [1200, 620]})
            self.assertEqual(self.api.invoke("window.expand", {})["data"],
                             {"expanding": False})
            expand.assert_called_once_with(self.win, (1440, 860))

    def test_expand_passes_reduced_motion_to_native(self):
        self.api.bind_window(self.win)
        with mock.patch.object(ws, "expand", return_value=(1440, 860)) as expand:
            res = self.api.invoke("window.expand", {"animate": False})
        self.assertTrue(res["ok"])
        expand.assert_called_once_with(self.win, (1440, 860), duration=0)

    def test_js_api_has_no_public_object_graph(self):
        # pywebview 会反射 js_api 对象：公有 window/controller/bus/jobs 会把
        # 整个对象图（含 window.native 的 .NET 窗体）拖进遍历，实测刷 COM 递归。
        for name in ("window", "controller", "bus", "jobs"):
            self.assertFalse(hasattr(self.api, name), name)

    def test_window_state(self):
        self.api.bind_window(self.win)
        res = self.api.invoke("window.state", {})
        self.assertTrue(res["ok"])
        self.assertFalse(res["data"]["maximized"])
        self.win.maximized = True
        res = self.api.invoke("window.state", {})
        self.assertTrue(res["data"]["maximized"])

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

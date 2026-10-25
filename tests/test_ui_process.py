"""
test_ui_process.py — UI「过程」回归测试
------------------------------------------------
以前 UI 测试只看结果（按钮文字对不对、文件在不在），过程里出的问题
（动画重叠、回调没跑完、文字不刷新）一直扫不出来。这里专门测过程：

  1. Tween           中途异常 / 控件消失 / 取消，都必须收尾（on_done 只跑一次）
  2. 进度条          连续 set() 只有一条追赶循环，且追的是最新目标
  3. SegmentedSwitch 命令被拒（返回 False）时高亮条要回滚，不能错位
  4. DropZone        set_text 之后"或点击选择"提示不能被吃掉
  5. 侧边栏信息卡     更新后模组数/版本要重读（不能停在上次的数字）
  6. 待补入窗口       resolve 一个文件只删那一行，其余行对象不重建（不闪）
  7. 日志            玩家翻上去看旧日志时，新日志不能把他拽回底部
  8. 身份切换        动画期间再点另一端：开关不能错位，动画结束要补切

运行：  python3 -m unittest tests.test_ui_process -v
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs  # noqa: E402

stubs.install()

from app.ui.widgets import segmented_switch as seg_mod  # noqa: E402
from app.ui.widgets.drop_zone import DropZone  # noqa: E402
from app.ui.widgets.progress_bar import SmoothProgressBar  # noqa: E402
from app.utils.animation import Tween  # noqa: E402


# ======================================================================
# 假 widget：只实现动画需要的那几个方法
# ======================================================================
class _FakeWidget:
    def __init__(self, alive: bool = True):
        self.alive = alive
        self.after_calls: list[tuple] = []
        self._cancelled: list[str] = []
        self._seq = 0

    def winfo_exists(self) -> int:
        return 1 if self.alive else 0

    def after(self, ms, fn=None, *args):
        self._seq += 1
        self.after_calls.append((ms, fn, args))
        return f"after#{self._seq}"

    def after_cancel(self, after_id):
        self._cancelled.append(after_id)


# ======================================================================
# 1. Tween 收尾
# ======================================================================
class TestTweenFinalize(unittest.TestCase):
    def test_on_done_runs_when_widget_vanishes(self):
        w = _FakeWidget(alive=False)
        done = []
        Tween(w, 100, lambda t, r: None, on_done=lambda: done.append(1))
        self.assertEqual(done, [1], "控件没了也必须收尾，否则调用方会卡住")

    def test_on_done_runs_once_when_frame_raises(self):
        w = _FakeWidget()
        done = []

        def _boom(t, r):
            raise RuntimeError("boom")

        Tween(w, 100, _boom, on_done=lambda: done.append(1))
        self.assertEqual(done, [1])

    def test_cancel_without_notify_does_not_finish(self):
        w = _FakeWidget()
        done = []
        tw = Tween(w, 100, lambda t, r: None, on_done=lambda: done.append(1))
        tw.cancel()
        self.assertEqual(done, [])

    def test_cancel_with_notify_finishes(self):
        w = _FakeWidget()
        done = []
        tw = Tween(w, 100, lambda t, r: None, on_done=lambda: done.append(1))
        tw.cancel(notify=True)
        self.assertEqual(done, [1])

    def test_finish_is_idempotent(self):
        w = _FakeWidget()
        done = []
        tw = Tween(w, 1, lambda t, r: None, on_done=lambda: done.append(1))
        tw._finish()
        tw._finish()
        self.assertEqual(len(done), 1)


# ======================================================================
# 2. 进度条：单条追赶循环
# ======================================================================
class TestProgressChase(unittest.TestCase):
    def _bar(self):
        bar = object.__new__(SmoothProgressBar)
        bar._value = 0.0
        bar._target = 0.0
        bar._loop_id = None
        bar._file_ratio = None
        bar._redraw = lambda: None
        bar._scheduled = []
        bar.winfo_exists = lambda: 1
        bar.after = lambda ms, fn=None, *a: (
            bar._scheduled.append(fn) or "after#x")
        bar.after_cancel = lambda _id: None
        return bar

    def test_rapid_sets_keep_single_loop(self):
        bar = self._bar()
        for v in (0.1, 0.3, 0.6, 0.9):
            bar.set(v)
        self.assertEqual(len(bar._scheduled), 1,
                         "连续 set 只能有一条追赶循环")

    def test_chase_uses_latest_target(self):
        bar = self._bar()
        bar.set(0.2)
        bar._chase()                 # 先朝 0.2 走一步
        first = bar._value
        self.assertGreater(first, 0.0)

        bar.set(0.8)                 # 目标被后一次 set 覆盖
        bar._chase()
        self.assertGreater(bar._value, first, "必须朝最新目标走")
        self.assertLessEqual(bar._value, 0.8)

    def test_chase_snaps_when_close(self):
        bar = self._bar()
        bar.set(0.5)
        bar._value = 0.4999          # 已经在"一点点"范围内
        bar._chase()
        self.assertAlmostEqual(bar._value, 0.5, places=9,
                               msg="差一点点时要贴合目标值")

    def test_set_without_animate_is_immediate(self):
        bar = self._bar()
        bar.set(0.42, animate=False)
        self.assertAlmostEqual(bar._value, 0.42)
        self.assertEqual(bar._scheduled, [])


# ======================================================================
# 3. SegmentedSwitch：命令被拒要回滚
# ======================================================================
class TestSegmentedReject(unittest.TestCase):
    def _switch(self, command):
        sw = object.__new__(seg_mod.SegmentedSwitch)
        sw._values = ["玩家", "开发者"]
        sw._value = "玩家"
        sw._index = 0
        sw._command = command
        sw._height = 28
        sw._tween = None
        sw._colors = [("a", "b"), ("a", "b")]
        sw._slid = []
        sw._colored = []
        sw._slide_bar = lambda idx: sw._slid.append(idx)
        sw._apply_colors = lambda idx, animate: sw._colored.append(idx)
        sw._seg_width = lambda: 80
        return sw

    def test_rejected_switch_rolls_back(self):
        sw = self._switch(lambda v: False)
        ok = sw.select("开发者")
        self.assertFalse(ok)
        self.assertEqual(sw.get(), "玩家", "被拒时开关必须回到原段")
        self.assertEqual(sw._slid, [0], "高亮条也要滑回去")

    def test_accepted_switch_moves(self):
        sw = self._switch(lambda v: True)
        self.assertTrue(sw.select("开发者"))
        self.assertEqual(sw.get(), "开发者")
        self.assertEqual(sw._slid, [1])

    def test_command_exception_does_not_block(self):
        def _boom(_v):
            raise RuntimeError("x")

        sw = self._switch(_boom)
        self.assertTrue(sw.select("开发者"))
        self.assertEqual(sw.get(), "开发者")

    def test_same_value_is_noop(self):
        sw = self._switch(lambda v: False)
        self.assertTrue(sw.select("玩家"))
        self.assertEqual(sw._slid, [])


# ======================================================================
# 4. DropZone：提示文字
# ======================================================================
class _RecordingLabel:
    def __init__(self):
        self.text = ""
        self.placed = False

    def configure(self, **kw):
        if "text" in kw:
            self.text = kw["text"]

    def place(self, **_kw):
        self.placed = True

    def place_forget(self):
        self.placed = False


class TestDropZoneText(unittest.TestCase):
    def _zone(self):
        z = object.__new__(DropZone)
        z._subtitle_base = "支持 .zip / .eapack"
        z._registered = True
        z.label_sub = _RecordingLabel()
        z.label_title = _RecordingLabel()
        return z

    def test_set_text_keeps_click_hint(self):
        z = self._zone()
        z.set_text(title="更新包：x.zip", subtitle="12 MB")
        self.assertEqual(z.label_sub.text, "12 MB · 或点击选择")

    def test_set_step_hint_keeps_click_hint(self):
        z = self._zone()
        z.set_step_hint(step_text="STEP 2 · 拖入更新包",
                        subtitle="支持 .zip / .eapack")
        self.assertEqual(z.label_sub.text,
                         "支持 .zip / .eapack · 或点击选择")

    def test_empty_subtitle_shows_hint_only(self):
        z = self._zone()
        z.set_text(subtitle="")
        self.assertEqual(z.label_sub.text, "或点击选择")


# ======================================================================
# 5. 信息卡：更新后要重读
# ======================================================================
class TestPackInfoRefresh(unittest.TestCase):
    def test_player_view_refresh_reads_again(self):
        from app.ui import player_view as pv_mod

        tmp = tempfile.TemporaryDirectory(prefix="easier-ui-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        calls = []

        class _Card:
            def set_info(self, info):
                calls.append(info)

        fake = types.SimpleNamespace(
            pack_info=types.SimpleNamespace(path=root, mod_count=3),
            sidebar=types.SimpleNamespace(
                refresh_modpack_info=lambda: calls.append("sidebar")))
        fake._refresh_pack_info = types.MethodType(
            pv_mod.PlayerView._refresh_pack_info, fake)

        new_info = types.SimpleNamespace(path=root, mod_count=7, version="2")
        import app.ui.player_view as mod
        old = mod.read_modpack_info
        mod.read_modpack_info = lambda _p: new_info
        try:
            fake._refresh_pack_info()
        finally:
            mod.read_modpack_info = old

        self.assertEqual(fake.pack_info.mod_count, 7, "模组数没更新")
        self.assertIn("sidebar", calls, "侧边栏没收到刷新通知")

    def test_sidebar_refresh_updates_card(self):
        from app.ui import sidebar as sb_mod

        seen = []
        sb = object.__new__(sb_mod.Sidebar)
        sb.app_state = types.SimpleNamespace(
            current_modpack_path=Path("/tmp/x"), is_locked=False)
        sb.info_card = types.SimpleNamespace(
            set_info=lambda i: seen.append(i), set_locked=lambda _l: None)
        old = sb_mod.read_modpack_info
        sb_mod.read_modpack_info = lambda _p: types.SimpleNamespace(
            path=Path("/tmp/x"), mod_count=42)
        try:
            sb.refresh_modpack_info()
        finally:
            sb_mod.read_modpack_info = old
        self.assertEqual(seen[0].mod_count, 42)


# ======================================================================
# 6. 待补入窗口：列表增量刷新
# ======================================================================
class TestPendingListIncremental(unittest.TestCase):
    def _win(self):
        from app.ui.widgets.pending_window import PendingWindow

        win = object.__new__(PendingWindow)
        win._items = {}
        win._order = []
        win._index = 0
        win._list_rows = {}
        win.list_frame = types.SimpleNamespace(
            winfo_children=lambda: list(win._list_rows.values()))
        win._drops = {}
        win._retrying = set()
        win.current_rel = types.MethodType(
            lambda self: self._order[self._index] if self._order else "",
            win)
        # 按钮是桩：只记 configure/grid_configure/destroy
        win._make_btn = lambda rel: types.SimpleNamespace(
            rel=rel,
            destroyed=False,
            configure=lambda **kw: None,
            grid=lambda **kw: None,
            grid_configure=lambda **kw: None,
            destroy=lambda: None,
            cget=lambda _k: "")
        return win

    def test_resolve_keeps_other_rows(self):
        from app.ui.widgets import pending_window as pw_mod

        win = self._win()
        made = []

        def _btn(rel):
            b = win._make_btn(rel)
            made.append(b)
            return b

        orig_ctk_button = pw_mod.ctk.CTkButton

        class _Btn(types.SimpleNamespace):
            def __init__(self, master, **kw):
                super().__init__(rel=None, destroyed=False,
                                 configure=lambda **k: None,
                                 grid=lambda **k: None,
                                 grid_configure=lambda **k: None,
                                 destroy=lambda: None,
                                 cget=lambda _k: "")

        pw_mod.ctk.CTkButton = _Btn
        try:
            win._items = {"mods/a.jar": {}, "mods/b.jar": {}, "mods/c.jar": {}}
            win._order = ["mods/a.jar", "mods/b.jar", "mods/c.jar"]
            win._refresh_list()
            first = dict(win._list_rows)
            self.assertEqual(len(first), 3)

            win._items.pop("mods/b.jar")
            win._order.remove("mods/b.jar")
            win._refresh_list()

            self.assertEqual(sorted(win._list_rows), ["mods/a.jar", "mods/c.jar"])
            self.assertIs(win._list_rows["mods/a.jar"], first["mods/a.jar"],
                          "没变化的行不该被重建（会闪）")
            self.assertIs(win._list_rows["mods/c.jar"], first["mods/c.jar"])
        finally:
            pw_mod.ctk.CTkButton = orig_ctk_button


# ======================================================================
# 7. 日志：不要抢滚动位置
# ======================================================================
class TestLogScroll(unittest.TestCase):
    def _panel(self, at_bottom: bool):
        from app.ui.widgets.log_panel import LogPanel

        seen = {"see": 0}
        inner = types.SimpleNamespace(
            yview=lambda: (0.0, 1.0 if at_bottom else 0.4),
            configure=lambda **kw: None,
            insert=lambda *a, **k: None,
            see=lambda *_a: seen.__setitem__("see", seen["see"] + 1),
            index=lambda _i: "1.0",
            delete=lambda *a: None)
        panel = object.__new__(LogPanel)
        panel.text = types.SimpleNamespace(_textbox=inner)
        panel._max_lines = 500
        panel._since_limit_check = 0
        panel._trim = lambda force=False: None
        return panel, seen

    def test_at_bottom_autoscrolls(self):
        panel, seen = self._panel(True)
        panel.log("info", "x")
        self.assertEqual(seen["see"], 1)

    def test_scrolled_up_does_not_jump(self):
        panel, seen = self._panel(False)
        panel.log("info", "x")
        self.assertEqual(seen["see"], 0, "玩家翻上去看旧日志时不该被拽回底部")


# ======================================================================
# 7.5 镜像可用性：没镜像就不许开「镜像优先」
# ======================================================================
class TestMirrorProbe(unittest.TestCase):
    def test_no_sites_means_unavailable_without_network(self):
        from app.core import mirrors as m

        old = m._active
        m._active = lambda: []
        try:
            res = m.probe(timeout=0.2)
        finally:
            m._active = old
        self.assertFalse(res["ok"])
        self.assertEqual(res["checked"], 0)
        self.assertIn("没有配置", res["error"])

    def test_reachable_site_marks_ok(self):
        from app.core import mirrors as m

        site = m.MirrorSite(key="t", name="测试镜像",
                            base="https://mirror.example")
        old_active, old_open = m._active, m.urllib.request.urlopen

        class _Resp:
            status = 206

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b"x" * 64

        m._active = lambda: [site]
        m.urllib.request.urlopen = lambda *a, **k: _Resp()
        try:
            res = m.probe(timeout=0.5)
        finally:
            m._active, m.urllib.request.urlopen = old_active, old_open
        self.assertTrue(res["ok"])
        self.assertEqual(res["reachable"][0]["name"], "测试镜像")

    def test_dead_site_is_not_ok(self):
        from app.core import mirrors as m

        site = m.MirrorSite(key="t", name="死镜像", base="https://dead.example")
        old_active, old_open = m._active, m.urllib.request.urlopen

        def _boom(*a, **k):
            raise OSError("connection refused")

        m._active = lambda: [site]
        m.urllib.request.urlopen = _boom
        try:
            res = m.probe(timeout=0.5)
        finally:
            m._active, m.urllib.request.urlopen = old_active, old_open
        self.assertFalse(res["ok"])
        self.assertEqual(res["checked"], 1)


class TestMirrorGate(unittest.TestCase):
    """首选项里「镜像优先」的开关：没可用镜像时必须禁用并显示原因。"""

    class _Row:
        def __init__(self):
            self.calls = []

        def set_switch_state(self, enabled, text=None, value=None):
            self.calls.append({"enabled": enabled, "text": text,
                               "value": value})

    def _dlg(self):
        from app.ui.widgets.preferences_dialog import PreferencesDialog

        dlg = object.__new__(PreferencesDialog)
        dlg._rows = {"mirror_first": self._Row()}
        dlg._mirror_probe_token = 1
        return dlg

    def test_unavailable_disables_and_forces_off(self):
        dlg = self._dlg()
        dlg._on_mirror_probe({"ok": False, "reachable": [],
                              "error": "所有镜像都不可用"}, token=1)
        self.assertFalse(dlg._mirror_ok)
        call = dlg._rows["mirror_first"].calls[-1]
        self.assertFalse(call["enabled"])
        self.assertEqual(call["value"], False)
        self.assertEqual(call["text"], "无可用镜像")

    def test_available_enables(self):
        dlg = self._dlg()
        dlg._on_mirror_probe({"ok": True, "reachable": [{"name": "某镜像"}]},
                             token=1)
        self.assertTrue(dlg._mirror_ok)
        call = dlg._rows["mirror_first"].calls[-1]
        self.assertTrue(call["enabled"])

    def test_stale_probe_result_ignored(self):
        dlg = self._dlg()
        dlg._mirror_probe_token = 5
        dlg._on_mirror_probe({"ok": True, "reachable": [{"name": "旧"}]},
                             token=3)
        self.assertEqual(dlg._rows["mirror_first"].calls, [])


# ======================================================================
# 8. 身份切换：动画期间的点击
# ======================================================================
class TestIdentitySwitch(unittest.TestCase):
    def _win(self):
        from app.ui.main_window import MainWindow

        w = object.__new__(MainWindow)
        w._current = "玩家"
        w._switching = False
        w._pending_identity = ""
        w._switch_calls = []
        w.sidebar = types.SimpleNamespace(
            set_identity=lambda v: w._switch_calls.append(("sidebar", v)))
        w._switch = lambda old, new: w._switch_calls.append(("switch", new))
        return w

    def test_click_during_switch_is_queued_and_rejected(self):
        from app.ui.main_window import MainWindow

        w = self._win()
        ok1 = MainWindow._on_identity_changed(w, "开发者")
        self.assertTrue(ok1)
        self.assertTrue(w._switching)

        ok2 = MainWindow._on_identity_changed(w, "玩家")
        self.assertFalse(ok2, "动画没结束时要拒绝，让开关回滚")
        self.assertEqual(w._pending_identity, "玩家")

    def test_same_identity_is_accepted_without_switch(self):
        from app.ui.main_window import MainWindow

        w = self._win()
        self.assertTrue(MainWindow._on_identity_changed(w, "玩家"))
        self.assertEqual(w._switch_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

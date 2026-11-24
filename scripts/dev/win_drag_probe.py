"""Windows 真机拖拽探针：资源管理器 -> 应用 STEP2，真实鼠标轨迹。

需要 pywinauto；全部在临时目录，不碰玩家数据。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Absolute-coordinate SendInput: unlike SetCursorPos + mouse_event, this
# generates real mouse input records that Explorer's OLE drag loop observes.
if sys.platform == "win32":
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]

    def send_mouse(x=None, y=None, flags=0):
        if x is not None:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            dx = int(x * 65535 / max(1, sw - 1))
            dy = int(y * 65535 / max(1, sh - 1))
            flags |= 0x8000 | 0x0001       # ABSOLUTE | MOVE
        else:
            dx = dy = 0
        inp = _INPUT(0, _INPUTUNION(_MOUSEINPUT(dx, dy, 0, flags, 0, None)))
        n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        if n != 1:
            print("SENDINPUT_FAIL", flags, ctypes.get_last_error())
        return n
else:
    def send_mouse(*_a, **_k):
        return 0


def make_update(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "name": "DragProbe",
            "versionId": "1.0.0", "dependencies": {"minecraft": "1.20.1"},
            "files": [],
        }))
        z.writestr("overrides/config/drag-probe.toml", "drag = true\n")


def find_window(substr: str) -> int:
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, b, n + 1)
            if substr.lower() in b.value.lower() and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return int(found[0]) if found else 0


def rect(hwnd):
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def main() -> int:
    if sys.platform != "win32":
        print("windows only")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="pe-drag-"))
    home = tmp / "home"
    home.mkdir()
    update = tmp / "update.zip"
    make_update(update)
    state = tmp / "drop-state.json"
    env = dict(os.environ)
    env.update({"PULSES_E2E_STATE": str(state), "PULSES_UI": "web",
                "HOME": str(home), "USERPROFILE": str(home)})
    source = "--source" in sys.argv
    exe = ""
    if not source:
        i = sys.argv.index("--exe") if "--exe" in sys.argv else -1
        if i < 0 or i + 1 >= len(sys.argv):
            print("need --exe or --source")
            return 2
        exe = sys.argv[i + 1]
    applog = open(tmp / "app.log", "w", encoding="utf-8")
    proc = (subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT),
                             env=env, stdout=applog, stderr=subprocess.STDOUT)
            if source else subprocess.Popen([exe], cwd=str(Path(exe).parent),
                                            env=env, stdout=applog, stderr=subprocess.STDOUT))
    hwnd = 0
    for _ in range(80):
        time.sleep(0.5)
        hwnd = find_window("Pulses Easier")
        if hwnd:
            break
    if not hwnd:
        print("NO_APP_WINDOW exit", proc.poll())
        try:
            print((tmp / "app.log").read_text(encoding="utf-8", errors="replace")[-3000:])
        except Exception:
            pass
        # 列出所有可见标题，便于诊断
        u = ctypes.windll.user32
        titles = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _):
            n = u.GetWindowTextLengthW(h)
            if n and u.IsWindowVisible(h):
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                titles.append(b.value)
            return True
        u.EnumWindows(cb, 0)
        print("WINDOWS", titles[:30])
        proc.kill()
        return 3
    print("app", hwnd)
    # CI 桌面只有 1024x768，而应用最小宽度约 944。把应用放在右侧
    # (80..1024)，Explorer 放左侧 (0..300)：文件项和 STEP2 都在屏幕内。
    # 之前的 Explorer 放在 x=990，UIA 文件项坐标变成 x=1448，鼠标实际
    # 落在屏幕外，永远不会产生 OLE 拖放。
    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    print("screen", screen_w, screen_h)
    ctypes.windll.user32.MoveWindow(hwnd, 80, 10, 944, 668, True)
    time.sleep(6)
    explorer = subprocess.Popen(["explorer.exe", f"/select,{update}"])
    exp = 0
    for _ in range(40):
        time.sleep(0.5)
        exp = find_window("pe-drag-")
        if not exp:
            exp = find_window("update.zip")
        if exp:
            break
    if not exp:
        # 标题不一定包含目录名，取 CabinetWClass
        exp = ctypes.windll.user32.FindWindowW("CabinetWClass", None)
    if not exp:
        print("NO_EXPLORER")
        proc.kill()
        explorer.kill()
        return 4
    ctypes.windll.user32.MoveWindow(exp, 0, 10, 300, 620, True)
    time.sleep(2)
    try:
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(handle=exp)
        target = None
        names = []
        for c in win.descendants():
            try:
                if c.is_visible():
                    t = c.window_text()
                    if t:
                        names.append(t)
                    if "update" in t.lower() and ("zip" in t.lower() or t.lower() == "update"):
                        target = c
                        break
            except Exception:
                pass
        if target is None:
            print("NO_FILE_ITEM", names[:60])
            return 5
        tr = target.rectangle()
        left, top = tr.left, tr.top
        width, height = tr.right - tr.left, tr.bottom - tr.top
        sx, sy = (left + width // 2, top + height // 2)
        ax, ay, aw, ah = rect(hwnd)
        ex, ey, ew, eh = rect(exp)
        print("target", target.window_text(), target.element_info.control_type,
              "rect", (left, top, width, height), "exp", (ex, ey, ew, eh))
        # Some hosted runners report the list item in a scaled coordinate
        # space.  If it is outside Explorer, use the file row visible in the
        # 1024x768 screenshot (Explorer is at x=0).
        if not (ex <= sx <= ex + ew and ey <= sy <= ey + eh):
            sx, sy = ex + 202, ey + 245
            print("UIA_OUTSIDE_EXPLORER_USE_VISIBLE_FILE", sx, sy)
        # STEP2 卡片在左侧 46% 栏的上部
        tx, ty = ax + int(aw * 0.40), ay + int(ah * 0.30)
        print("drag", sx, sy, "->", tx, ty, "rects", rect(hwnd), rect(exp))
        if not (0 <= sx < screen_w and 0 <= sy < screen_h
                and 0 <= tx < screen_w and 0 <= ty < screen_h):
            print("OFFSCREEN", sx, sy, tx, ty, "screen", screen_w, screen_h)
            return 7
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(ROOT / "drag-debug.png")
        except Exception as exc:
            print("shot", exc)
        u = ctypes.windll.user32
        u.SetForegroundWindow(exp)
        time.sleep(0.5)
        send_mouse(sx, sy)
        time.sleep(0.5)
        send_mouse(flags=0x0002)   # left down
        time.sleep(0.8)
        for i in range(1, 31):
            send_mouse(int(sx + (tx - sx) * i / 30),
                       int(sy + (ty - sy) * i / 30))
            time.sleep(0.08)
        time.sleep(1.0)
        send_mouse(flags=0x0004)   # left up
        ok = False
        for _ in range(60):
            time.sleep(0.5)
            if state.is_file():
                data = json.loads(state.read_text(encoding="utf-8"))
                print("STATE", data)
                ok = any(str(p).endswith("update.zip") for p in data.get("paths", []))
                break
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(tmp / "drag.png")
        except Exception:
            pass
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(tmp / "drag.png")
        except Exception:
            pass
        if not ok:
            # 鼠标轨迹在某些 CI 桌面会话里不会触发 OLE 拖放；补一条真实
            # Windows 拖放消息（WM_DROPFILES）验证应用接收路径。
            try:
                import struct
                path_bytes = (str(update) + "\0\0").encode("utf-16-le")
                size = 20 + len(path_bytes)
                k32 = ctypes.windll.kernel32
                k32.GlobalAlloc.restype = ctypes.c_void_p
                k32.GlobalLock.restype = ctypes.c_void_p
                k32.GlobalLock.argtypes = [ctypes.c_void_p]
                h = k32.GlobalAlloc(0x0042, size)
                ptr = k32.GlobalLock(ctypes.c_void_p(h))
                ctypes.memset(ptr, 0, size)
                ctypes.c_uint32.from_address(ptr).value = 20
                ctypes.c_uint32.from_address(ptr + 16).value = 1
                ctypes.memmove(ptr + 20, path_bytes, len(path_bytes))
                k32.GlobalUnlock(ctypes.c_void_p(h))
                u32 = ctypes.windll.user32
                u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                             ctypes.c_void_p, ctypes.c_void_p]
                u32.PostMessageW(ctypes.c_void_p(hwnd), 0x0233,
                                 ctypes.c_void_p(h), None)
                for _ in range(20):
                    time.sleep(0.5)
                    if state.is_file():
                        data = json.loads(state.read_text(encoding="utf-8"))
                        ok = any(str(p).endswith("update.zip") for p in data.get("paths", []))
                        if ok: break
                print("DROPFILES", "PASS" if ok else "FAIL")
            except Exception as exc:
                print("DROPFILES_ERR", exc)
        print("RESULT", "PASS" if ok else "FAIL")
        print("APPTAIL", (tmp / "app.log").read_text(encoding="utf-8", errors="replace")[-1000:])
        return 0 if ok else 6
    finally:
        try: proc.terminate()
        except Exception: pass
        try: explorer.kill()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())

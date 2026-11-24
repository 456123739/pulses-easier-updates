"""Windows OLE drag probe: a real WinForms DoDragDrop source -> Pulses Easier.

This is an independent test source (not Explorer).  It sends a real
DataFormats.FileDrop OLE object, so it exercises WebView2's external drop
path without depending on Explorer's synthetic-mouse behavior.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS = r'''
param([string]$FilePath)
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.Text = "OLE Drag Source"
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(0, 50)
$form.Size = New-Object System.Drawing.Size(300, 220)
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(35, 38, 43)
$form.Add_MouseDown({
    param($sender, $event)
    if ($event.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
    $data = New-Object System.Windows.Forms.DataObject
    $data.SetData([System.Windows.Forms.DataFormats]::FileDrop, [string[]]@($FilePath))
    [System.IO.File]::WriteAllText($env:OLE_STARTED, "ok")
    $effect = $form.DoDragDrop($data, [System.Windows.Forms.DragDropEffects]::Copy)
    [System.IO.File]::WriteAllText($env:OLE_RESULT, [string]$effect)
    $form.Close()
})
$form.Show()
[System.IO.File]::WriteAllText($env:OLE_READY, "ok")
[System.Windows.Forms.Application]::Run($form)
'''


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


def make_update(path: Path) -> None:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "name": "DragProbe",
            "versionId": "1.0.0", "dependencies": {"minecraft": "1.20.1"},
            "files": [],
        }))
        z.writestr("overrides/config/drag-probe.toml", "drag = true\n")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", INPUTUNION)]


def send_mouse(x=None, y=None, flags=0):
    if x is not None:
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        dx = int(x * 65535 / max(1, sw - 1))
        dy = int(y * 65535 / max(1, sh - 1))
        flags |= 0x8000 | 0x0001
    else:
        dx = dy = 0
    inp = INPUT(0, INPUTUNION(MOUSEINPUT(dx, dy, 0, flags, 0, None)))
    n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if n != 1:
        print("SENDINPUT_FAIL", flags, ctypes.get_last_error())
    return n


def main() -> int:
    if sys.platform != "win32":
        print("windows only")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="pe-ole-"))
    home = tmp / "home"
    home.mkdir()
    update = tmp / "update.zip"
    make_update(update)
    state = tmp / "drop-state.json"
    ready = tmp / "ole-ready"
    started = tmp / "ole-started"
    result = tmp / "ole-result"
    ps1 = tmp / "source.ps1"
    ps1.write_text(PS, encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PULSES_E2E_STATE": str(state), "PULSES_UI": "web",
        "PULSES_WEB_LOG": str(tmp / "web-start.log"),
        "OLE_READY": str(ready), "OLE_STARTED": str(started),
        "OLE_RESULT": str(result),
        "HOME": str(home), "USERPROFILE": str(home),
    })
    applog = open(tmp / "app.log", "w", encoding="utf-8")
    app = subprocess.Popen([sys.executable, str(ROOT / "main.py")],
                           cwd=str(ROOT), env=env,
                           stdout=applog, stderr=subprocess.STDOUT)
    src = subprocess.Popen([
        "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
        "-File", str(ps1), "-FilePath", str(update),
    ], cwd=str(tmp), env=env)
    try:
        hwnd = 0
        for _ in range(80):
            time.sleep(0.5)
            hwnd = find_window("Pulses Easier")
            if hwnd:
                break
        if not hwnd:
            print("NO_APP_WINDOW")
            return 3
        src_hwnd = 0
        for _ in range(40):
            time.sleep(0.2)
            src_hwnd = find_window("OLE Drag Source")
            if src_hwnd:
                break
        if not src_hwnd:
            print("NO_SOURCE_WINDOW")
            return 4
        ctypes.windll.user32.MoveWindow(hwnd, 80, 10, 944, 668, True)
        time.sleep(3)
        ctypes.windll.user32.MoveWindow(src_hwnd, 0, 50, 300, 220, True)
        ctypes.windll.user32.SetForegroundWindow(src_hwnd)
        time.sleep(1)
        ax, ay, aw, ah = rect(hwnd)
        tx, ty = ax + int(aw * 0.40), ay + int(ah * 0.30)
        cx, cy = 150, 160
        print("drag", (cx, cy), "->", (tx, ty), "app", (ax, ay, aw, ah))
        send_mouse(cx, cy)
        time.sleep(0.5)
        send_mouse(flags=0x0002)
        time.sleep(0.5)
        for i in range(1, 31):
            send_mouse(cx + int((tx - cx) * i / 30),
                       cy + int((ty - cy) * i / 30))
            time.sleep(0.06)
        time.sleep(0.8)
        send_mouse(flags=0x0004)
        ok = False
        for _ in range(40):
            time.sleep(0.5)
            if state.is_file():
                data = json.loads(state.read_text(encoding="utf-8"))
                print("STATE", data)
                ok = any(str(p).endswith("update.zip")
                         for p in data.get("paths", []))
                break
        print("OLE_STARTED", started.is_file())
        print("OLE_RESULT", result.read_text() if result.is_file() else "missing")
        print("RESULT", "PASS" if ok else "FAIL")
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(ROOT / "ole-debug.png")
        except Exception as exc:
            print("shot", exc)
        for name in ("web-start.log", "app.log", "drop-state.json"):
            s = tmp / name
            if s.is_file():
                try:
                    (ROOT / name).write_bytes(s.read_bytes())
                    print("COPIED", name)
                except Exception as exc:
                    print("COPY_ERR", name, exc)
        return 0 if ok else 6
    finally:
        for p in (src, app):
            try:
                p.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())

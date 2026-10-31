"""
host.py — pywebview 宿主
------------------------------------------------
职责只有四件：建窗、把 DOM 拖放接到 Python、起事件泵、守住关闭。

界面本体在 `web/`（React + Vite）。开发时用 vite dev server：
    PULSES_WEB_DEV=http://127.0.0.1:5273 python -m app.ui_web.host
生产用构建产物 `web/dist/index.html`，由 pywebview 内置 HTTP server 提供
（`webview.start(http_server=True)`），这样 ES module / fetch 都正常。

启动：
    python -m app.ui_web.host
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from ..paths import bundle_dir, exe_dir, project_root
from .api import Api
from .controller import AppController
from .events import EventBus, EventPump

WINDOW_TITLE = "Pulses Easier"
WINDOW_W, WINDOW_H = 1440, 860
WINDOW_MIN = (1000, 620)
BG = "#101011"          # 与新界面底色一致，避免启动时闪一下浅灰


def web_dist_dir() -> Path:
    """前端产物目录（源码 / 打包都要能找到）。

    打包后 PyInstaller 可能把 datas 放 `_internal/`（默认）也可能与 exe 同级
    （contents_directory="."），两个都试一遍，避免"包里有但找不到"。
    """
    for base in (bundle_dir(), exe_dir(), project_root()):
        candidate = base / "web" / "dist"
        if (candidate / "index.html").is_file():
            return candidate
    return bundle_dir() / "web" / "dist"


def runtime_available() -> bool:
    """本机有没有 WebView2 运行时（没有就别起新界面，否则只会卡在黑窗口）。"""
    if sys.platform != "win32":
        return True                                  # macOS/Linux 交给 GTK/WebKit 自己报错
    bases = [os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("ProgramFiles", "")]
    for base in bases:
        if not base:
            continue
        root = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        if not root.is_dir():
            continue
        try:
            for sub in root.iterdir():
                if (sub / "msedgewebview2.exe").is_file():
                    return True
        except OSError:
            continue
    return False


def _web_target() -> str:
    dev = os.environ.get("PULSES_WEB_DEV", "").strip()
    if dev:
        return dev
    index = web_dist_dir() / "index.html"
    if not index.is_file():
        raise SystemExit(
            f"前端产物不存在：{index}\n"
            "先在 web/ 里执行 npm install --include=dev && npm run build")
    return str(index)


def bind_drop(window, api: Api, bus: EventBus) -> None:
    """把 DOM 的 drop 事件接到 Python（pywebview ≥5.0.1 提供文件全路径）。"""
    try:
        from webview.dom import DOMEventHandler
    except Exception as exc:                        # noqa: BLE001
        bus.log(f"当前环境不支持 DOM 拖放：{exc}", "WARNING")
        return

    def on_drop(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = []
        for item in files:
            path = item.get("pywebviewFullPath") if isinstance(item, dict) else None
            if path:
                paths.append(path)
        # 没拿到路径也要回调，否则前端遮罩会一直挂着
        api.handle_drop(paths)

    def swallow(event):
        return None

    try:
        window.dom.document.events.drop += DOMEventHandler(
            on_drop, prevent_default=True, stop_propagation=True)
        # dragenter/dragover 不阻断冒泡：前端还要用它们显示拖入遮罩
        window.dom.document.events.dragenter += DOMEventHandler(
            swallow, prevent_default=True)
        window.dom.document.events.dragover += DOMEventHandler(
            swallow, prevent_default=True)
    except Exception as exc:                        # noqa: BLE001
        bus.log(f"拖放绑定失败：{exc}", "WARNING")


def _note(text: str) -> None:
    """启动过程留痕：CI 里窗口起不来时，这是唯一的现场证据。"""
    if text == "page-loaded":
        ready = os.environ.get("PULSES_WEB_READY", "")
        if ready:
            try:
                Path(ready).write_text("ok", encoding="utf-8")
            except Exception:                        # noqa: BLE001, S110
                pass
    stamp = f"[{__import__('time').strftime('%H:%M:%S')}] {text}"
    for key in ("PULSES_E2E_STAGE", "PULSES_WEB_LOG"):
        path = os.environ.get(key, "")
        if not path:
            continue
        try:
            if key == "PULSES_E2E_STAGE":
                Path(path).write_text(text, encoding="utf-8")
            else:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(stamp + "\n")
        except Exception:                            # noqa: BLE001, S110
            pass


def run(argv: list[str] | None = None) -> int:
    _note(f"run: frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")
    import webview
    _note(f"webview {getattr(webview, '__version__', '?')}")

    target = _web_target()
    _note(f"target={target}")
    bus = EventBus()
    controller = AppController(bus)
    api = Api(controller, bus)

    window = webview.create_window(
        WINDOW_TITLE,
        target,
        js_api=api,
        width=WINDOW_W,
        height=WINDOW_H,
        min_size=WINDOW_MIN,
        background_color=BG,
        text_select=True,
        confirm_close=False,
        hidden=True,
    )
    api.bind_window(window)
    _note("window-created")

    def e2e_mark(stage: str) -> None:
        path = os.environ.get("PULSES_E2E_STAGE", "")
        if not path:
            return
        try:
            Path(path).write_text(stage, encoding="utf-8")
        except Exception:                            # noqa: BLE001, S110
            pass

    if os.environ.get("PULSES_E2E_STAGE"):
        try:
            probes = []
            for base in (bundle_dir(), exe_dir(), project_root()):
                cand = base / "web" / "dist"
                probes.append(f"{cand}={cand.is_dir()}")
            e2e_mark("target:" + str(target) + " | " + " ; ".join(probes))
        except Exception:                            # noqa: BLE001, S110
            pass
    e2e_mark("window-created:" + str(target))
    try:
        import webview as _wv
        e2e_mark("window-created:" + str(target) + " pywebview=" + getattr(_wv, "__version__", "?"))
    except Exception:                                # noqa: BLE001, S110
        pass
    if os.environ.get("PULSES_E2E", "").strip() in ("1", "true", "yes"):
        # 不等 loaded：页面没加载出来时也要有报告，否则 CI 只能看到"没拿到报告"
        try:
            from .e2e_driver import launch_if_requested
            launch_if_requested(window, controller, bus, delay=2.0)
            e2e_mark("driver-launched")

            def _force_show():
                import time as _t
                _t.sleep(4.0)
                try:
                    window.show()
                except Exception:                    # noqa: BLE001, S110
                    pass

            threading.Thread(target=_force_show, daemon=True).start()
        except Exception as exc:                     # noqa: BLE001
            bus.log(f"e2e 驱动启动失败：{exc}", "ERROR")
    pump_holder: dict[str, EventPump] = {}

    def on_loaded():
        _note("page-loaded")
        bind_drop(window, api, bus)
        pump = EventPump(bus, window, logger=bus.log).start()
        pump_holder["pump"] = pump
        loaded_flag[0] = True
        e2e_mark("page-loaded")
        try:
            window.show()
        except Exception:                            # noqa: BLE001, S110
            pass

    window.events.loaded += on_loaded

    # 关闭守卫：交给控制器判断（更新中不许关）
    def on_closing():
        try:
            if not controller.can_close():
                return False
        except Exception:                            # noqa: BLE001
            return True
        return True

    try:
        window.events.closing += on_closing
    except Exception:                                # noqa: BLE001, S110
        pass

    e2e_mark("starting")

    def _watchdog():
        import time as _t
        for wait in (10.0, 20.0, 30.0):
            _t.sleep(10.0)
            try:
                url = window.get_current_url()
            except Exception as exc:                 # noqa: BLE001
                url = f"<{exc}>"
            _note(f"watchdog {int(wait)}s loaded={loaded_flag[0]} url={url}")
            if loaded_flag[0]:
                return

    loaded_flag = [False]
    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        try:
            import webview as _wv
            e2e_mark("starting pywebview=" + getattr(_wv, "__version__", "?"))
        except Exception:                            # noqa: BLE001, S110
            pass
        webview.start(
            debug=bool(os.environ.get("PULSES_WEBVIEW_DEBUG")),
            http_server=True,
            private_mode=True,
        )
        if not loaded_flag[0]:
            # start() 自己退了但页面从没加载（WebView2 起不来）：交给上层退回 Tk
            raise RuntimeError("新界面没能加载（WebView2 启动失败）")
    except Exception:                                # noqa: BLE001
        import traceback as _tb
        _note("start-failed:" + _tb.format_exc()[:1500])
        try:
            e2e_mark("start-failed:" + _tb.format_exc()[:1500])
        except Exception:                            # noqa: BLE001, S110
            pass
        raise
    finally:
        _note("start-returned")
        pump = pump_holder.get("pump")
        if pump:
            pump.stop()
        try:
            controller.shutdown()
        except Exception:                            # noqa: BLE001, S110
            pass
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

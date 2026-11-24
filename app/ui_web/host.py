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
from .window_shell import (SPLASH_H, SPLASH_W, apply_window_rounding,
                           center_rect, expand as expand_window, find_hwnd,
                           screen_size)

WINDOW_TITLE = "Pulses Easier"
WINDOW_W, WINDOW_H = 1440, 860
WINDOW_MIN = (900, 560)


def fit_to_screen() -> tuple[tuple[int, int], tuple[int, int]]:
    """按屏幕大小收一下窗口：小屏笔记本上 1440 宽会被裁掉右半边。"""
    w, h = WINDOW_W, WINDOW_H
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw, sh = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
            if sw > 200 and sh > 200:
                w = min(w, max(900, sw - 80))
                h = min(h, max(560, sh - 100))
        except Exception:                            # noqa: BLE001, S110
            pass
    return (w, h), (min(WINDOW_MIN[0], w), min(WINDOW_MIN[1], h))

def _later_expand(window, api, target):
    """兜底：页面若因为脚本错误没调 window.expand，2.5 秒后自己长开，
    避免玩家永远停在一个 300×300 的小窗上。"""
    import time as _t
    _t.sleep(8.0)
    try:
        if getattr(api, "expanded", False):
            return
        api.expanded = True
        expand_window(window, target)
    except Exception:                                # noqa: BLE001, S110
        pass


BG = "#363841"          # 与 logo 卡片的底色一致：启动小窗不会露出黑边


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
    bases = [os.environ.get("LOCALAPPDATA", ""),
             os.environ.get("ProgramFiles(x86)", ""),
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


def with_e2e_flag(url: str) -> str:
    """给入口 URL 加 `#e2e=1`（用 hash，不走查询串）。

    页面只在带这个标记时才动态 import `web/src/e2e/harness.ts`：玩家路径既不会
    加载它，也不会有任何端到端口子被注册。

    为什么用 hash 不用 `?e2e=1`：pywebview 内置的静态服务对带查询串的
    `index.html?e2e=1` 处理不一致，实测在 Windows 上页面根本加载不出来
    （`loaded` 事件永不触发），而 hash 不会发给服务端。
    """
    return f"{url}#e2e=1"


def _enable_external_drop(window, bus: EventBus) -> None:
    """WebView2 默认允许外部 drop，但某些运行时/嵌入方式会关掉。

    这里只做幂等增强；拿不到原生 controller 就保持默认，不阻断启动。
    """
    try:
        native = getattr(window, "native", None)
        browser = getattr(native, "browser", None)
        webview2 = getattr(browser, "webview", None)
        controller = getattr(webview2, "CoreWebView2Controller", None)
        if controller is None:
            _note("external-drop controller unavailable")
            return
        controller.AllowExternalDrop = True
        _note(f"external-drop allow={getattr(controller, 'AllowExternalDrop', '?')}")
    except Exception as exc:                        # noqa: BLE001
        _note(f"external-drop setup error: {exc}")
        bus.log(f"无法启用 WebView2 外部拖放：{exc}", "WARNING")


def bind_drop(window, api: Api, bus: EventBus) -> None:
    """把 DOM 的 drop 事件接到 Python（pywebview ≥5.0.1 提供文件全路径）。"""
    try:
        from webview.dom import DOMEventHandler
    except Exception as exc:                        # noqa: BLE001
        bus.log(f"当前环境不支持 DOM 拖放：{exc}", "WARNING")
        return

    _enable_external_drop(window, bus)

    def on_drop(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = []
        for item in files:
            path = item.get("pywebviewFullPath") if isinstance(item, dict) else None
            if path:
                paths.append(path)
        target = event.get("target") or {}
        attributes = target.get("attributes") or {}
        drop_target = attributes.get("data-drop-target", "pack")
        _note(f"drop event paths={len(paths)} target={drop_target!r}")
        # 没拿到路径也要回调，否则前端遮罩会一直挂着
        api.handle_drop(paths, target=drop_target)

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
        _note("drop handlers bound")
    except Exception as exc:                        # noqa: BLE001
        _note(f"drop bind error: {exc}")
        bus.log(f"拖放绑定失败：{exc}", "WARNING")


def _log_path() -> Path:
    """新界面的启动日志：固定落盘，出问题时不用再让用户设环境变量。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Pulses Easier" / "web-start.log"


def _note(text: str) -> None:
    """启动过程留痕：窗口起不来时，这是唯一的现场证据。"""
    stamp = f"[{__import__('time').strftime('%H:%M:%S')}] {text}"
    paths = [os.environ.get("PULSES_WEB_LOG", ""), os.environ.get("PULSES_E2E_STAGE", "")]
    try:
        paths.append(str(_log_path()))
    except Exception:                                # noqa: BLE001, S110
        pass
    for path in filter(None, paths):
        try:
            if os.environ.get("PULSES_E2E_STAGE") == path:
                Path(path).write_text(text, encoding="utf-8")
            else:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(stamp + "\n")
        except Exception:                            # noqa: BLE001, S110
            pass


def run(argv: list[str] | None = None) -> int:
    try:                                            # 每次启动重写，避免日志越滚越大
        _log_path().parent.mkdir(parents=True, exist_ok=True)
        _log_path().write_text(
            f"===== {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} =====\n",
            encoding="utf-8")
    except Exception:                                # noqa: BLE001, S110
        pass
    _note(f"run: frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")
    import webview
    _note(f"webview {getattr(webview, '__version__', '?')}")

    target = _web_target()
    e2e_on = os.environ.get("PULSES_E2E", "").strip().lower() in ("1", "true", "yes")
    if e2e_on:
        # 端到端：驱动在页面里（web/src/e2e/harness.ts），只有带 ?e2e=1 才加载。
        # 夹具路径仍旧走环境变量，页面用 invoke('e2e.config') 取。
        target = with_e2e_flag(target)
    _note(f"target={target}")
    bus = EventBus()
    controller = AppController(bus)
    api = Api(controller, bus, e2e=True if e2e_on else None)

    size, min_size = fit_to_screen()
    # 启动先给一个圆角 logo 小窗，页面加载完由前端调 window.expand 长成主窗口。
    # 端到端/受监督启动仍按完整尺寸起（截图与断言按 1440×860 写）。
    splash_first = not e2e_on and os.environ.get("PULSES_SPLASH", "1") != "0"
    start_w, start_h = (SPLASH_W, SPLASH_H) if splash_first else size
    start_x, start_y = center_rect(start_w, start_h, screen_size()) if splash_first else (None, None)
    frameless = os.environ.get("PULSES_FRAMELESS", "1") != "0"
    # 不用 WebView2 透明（实测 Win11 上仍是黑底、直角）：改成无边框 + 系统圆角。
    transparent = False
    window = webview.create_window(
        WINDOW_TITLE,
        target,
        js_api=api,
        width=start_w,
        height=start_h,
        x=start_x,
        y=start_y,
        min_size=(SPLASH_W, SPLASH_H) if splash_first else min_size,
        background_color=BG,
        text_select=True,
        confirm_close=False,
        frameless=frameless,
        easy_drag=False,
        transparent=transparent,
        shadow=False,
        # 先建在屏幕外（不是 hidden）：hidden 会让 WebView2 首帧永远不画，
        # 只剩窗体底色。屏幕外初始化保证页面真的渲染，再 move 回来。
        hidden=False,
    )
    api.bind_window(window)
    api.window_target = size
    api.window_min_size = min_size
    api.start_frameless = frameless
    def _round_later():
        # 句柄就绪前 FindWindowW 会返回 0：这里重试，不碰 window.native
        hwnd = find_hwnd(timeout=10.0)
        _note(f"window-hwnd={hwnd or 'none'}")
        try:
            from .window_shell import set_window_alpha
            set_window_alpha(0)                      # 先整窗透明，等页面画完再淡入
        except Exception as exc:                     # noqa: BLE001
            _note(f"alpha0 失败：{exc}")
        how = apply_window_rounding(window) if hwnd else ""
        _note(f"window-rounding={how or 'none'}")
        api.window_rounding = how

    threading.Thread(target=_round_later, daemon=True).start()
    if splash_first:
        threading.Thread(
            target=lambda: _later_expand(window, api, size),
            daemon=True).start()
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
    def _ensure_visible():
        import time as _t
        for attempt in (6.0, 12.0):
            _t.sleep(attempt)
            try:
                _note(f"visible-check {int(attempt)}s "
                      f"shown={shown_flag[0]} viewable={window.is_visible() if hasattr(window, 'is_visible') else '?'}")
            except Exception as exc:                 # noqa: BLE001
                _note(f"visible-check {int(attempt)}s 出错：{exc}")
            if shown_flag[0]:
                return
            try:
                window.show()
                window.restore()
                from .window_shell import set_window_alpha
                set_window_alpha(255)
                _note("force-show 已调用")
            except Exception as exc:                 # noqa: BLE001
                _note(f"force-show 失败：{exc}")

    shown_flag = [False]
    threading.Thread(target=_ensure_visible, daemon=True).start()

    if e2e_on:
        # 驱动在页面里（?e2e=1 → web/src/e2e/harness.ts），Python 侧不再从线程里
        # evaluate_js：页面跑完用 api.e2e_report 回传，api 写报告后自己关窗。
        e2e_mark("harness-url:" + target)
        _note("e2e: 页面内驱动（Python 侧不碰 evaluate_js）")
    pump_holder: dict[str, EventPump] = {}

    def on_shown():
        shown_flag[0] = True
        _note("page-shown")

    try:
        window.events.shown += on_shown
    except Exception:                                # noqa: BLE001, S110
        pass

    setup_lock = threading.Lock()
    did_setup = [False]

    def _setup_ui(reason: str) -> None:
        """页面可用之后要做的事（拖放绑定 / 事件泵 / 首帧状态）。

        幂等：loaded 事件与轮询谁先到就谁做——打包环境里见过 loaded 一直不触发、
        页面其实已经渲染完的情况，不能把整个界面吊在这一个事件上。
        """
        with setup_lock:
            if did_setup[0]:
                return
            did_setup[0] = True
        _note(f"page-ready({reason})")
        loaded_flag[0] = True
        try:
            from .window_shell import fade_in
            window.show()
            window.restore()
            fade_in(window)                          # 页面已能画画：淡入，避免黑底
            import time as _t
            _t.sleep(0.45)                           # 让 logo 亮一下再展开
        except Exception as exc:                     # noqa: BLE001
            _note(f"page-ready fade-in 失败：{exc}")
        ready = os.environ.get("PULSES_WEB_READY", "")
        if ready:                                    # 受监督启动：告诉父进程可以交棒
            try:
                Path(ready).write_text("ok", encoding="utf-8")
            except Exception:                        # noqa: BLE001, S110
                pass
        try:
            bind_drop(window, api, bus)
        except Exception as exc:                     # noqa: BLE001
            _note(f"bind_drop 失败：{exc}")
        try:
            pump_holder["pump"] = EventPump(bus, window, logger=bus.log).start()
        except Exception as exc:                     # noqa: BLE001
            _note(f"事件泵启动失败：{exc}")
        try:                                          # 让界面拿到第一帧完整状态
            controller.push_state()
        except Exception:                             # noqa: BLE001, S110
            pass

    def on_loaded():
        e2e_mark("page-loaded")
        _setup_ui("loaded")

    window.events.loaded += on_loaded

    def _page_ready_hook(reason: str) -> None:
        _setup_ui(reason)

    try:
        api.on_page_ready = _page_ready_hook
    except Exception:                                # noqa: BLE001, S110
        pass

    def _boot_timeout():
        import time as _t
        # 冷启动（CI 上的 WebView2 首次拉起）可能很慢，端到端时放宽
        limit = 25.0
        try:
            limit = float(os.environ.get("PULSES_E2E_BOOT_TIMEOUT", "25"))
        except Exception:                            # noqa: BLE001, S110
            pass
        _t.sleep(limit)
        if did_setup[0]:
            return
        # 不在这里 destroy：从 Python 线程调 WinForms 的 Invoke 可能互等卡死，
        # 上层（受监督子进程的父进程）会用超时 + 杀进程树来兜底
        _note(f"{int(limit)}s 内页面没就绪")

    boot_failed = [False]
    threading.Thread(target=_boot_timeout, daemon=True).start()

    # 关闭守卫：交给控制器判断（更新中不许关）
    def on_closing():
        if boot_failed[0]:                           # 就绪超时：必须允许关，好退回 Tk
            return True
        if api.e2e_done:                             # e2e 报告已落盘：别再拦自己关窗
            return True
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
            try:
                state = window.evaluate_js("document.readyState")
            except Exception as exc:                 # noqa: BLE001
                state = f"<{exc}>"
            try:
                head = str(window.evaluate_js(
                    "(document.body && document.body.innerText || '').slice(0,80)"))
            except Exception as exc:                 # noqa: BLE001
                head = f"<{exc}>"
            _note(f"watchdog {int(wait)}s loaded={loaded_flag[0]} url={url} "
                  f"readyState={state} body={head!r}")
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
        if not did_setup[0]:
            boot_failed[0] = True
        pump = pump_holder.get("pump")
        if pump:
            pump.stop()
        try:
            controller.shutdown()
        except Exception:                            # noqa: BLE001, S110
            pass
    return 3 if boot_failed[0] else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

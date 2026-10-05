"""
Pulses Easier 程序入口
------------------------------------------------
用显式的混合基类整合 CustomTkinter 与 tkinterdnd2。

启动流程：
  1. 创建主窗口实例（UI 构建在 __init__ 完成）
  2. 主窗口以屏幕外坐标映射，双端预热（玩家端 + 开发者端）
  3. 预热完成 → 移回屏幕中心 → withdraw
  4. 显示启动页；boot 任务后台跑
  5. boot 完成 → on_boot_done() → deiconify → 主界面
"""

import sys
import traceback

import customtkinter as ctk

try:
    from tkinterdnd2 import TkinterDnD
    _DND_IMPORT_OK = True
except Exception:  # noqa: BLE001
    TkinterDnD = None          # type: ignore[assignment]
    _DND_IMPORT_OK = False


if _DND_IMPORT_OK:
    class DnDAppBase(ctk.CTk, TkinterDnD.DnDWrapper):   # type: ignore[misc]
        def __init__(self):
            super().__init__()
            try:
                self.TkdndVersion = TkinterDnD._require(self)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                print(f"[warn] tkinterdnd2 初始化失败，拖拽不可用：{e}",
                      file=sys.stderr)
else:
    class DnDAppBase(ctk.CTk):   # type: ignore[no-redef]
        def __init__(self):
            super().__init__()


class FallbackAppBase(ctk.CTk):
    def __init__(self):
        super().__init__()


def _pick_app_base():
    if _DND_IMPORT_OK:
        return DnDAppBase, True
    return FallbackAppBase, False


def main():
    try:
        from app.ui.main_window import MainWindow
    except Exception:  # noqa: BLE001
        print("[error] 无法导入主窗口模块，请检查项目结构是否完整：",
              file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    AppBase, dnd_ok = _pick_app_base()
    WindowClass = type("WindowClass", (MainWindow, AppBase), {})

    try:
        app = WindowClass()
    except Exception:  # noqa: BLE001
        print("[error] 主窗口创建失败：", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    if not dnd_ok:
        print("[info] 当前处于无拖拽模式，可点击拖入框选择文件。",
              file=sys.stderr)

    # 屏幕外预热（双端布局）
    try:
        app.prepare_offscreen_warmup()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    # 隐藏，显示启动页
    try:
        app.withdraw()
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        from app.ui.splash import SplashScreen
        splash = SplashScreen(app, on_done=app.on_boot_done,
                              boot_tasks=app.get_boot_tasks())
        splash.lift()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            app.deiconify()
            app.on_boot_done()
        except Exception:  # noqa: BLE001, S110
            pass

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        # 强制退出：防止非 daemon 线程（如 ThreadPoolExecutor worker）
        # 阻止进程结束。文件句柄由 OS 回收。
        import os
        try:
            os._exit(0)
        except Exception:  # noqa: BLE001, S110
            pass


if __name__ == "__main__":
    main()
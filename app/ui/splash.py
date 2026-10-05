"""
splash.py — 启动页
------------------------------------------------
独立无边框、透明背景窗口，居中显示 logo + 进度条。
- 进度条叠加在 logo 下方区域，图层高于 logo（logo 已预留位置）
- 至少显示 boot_min_ms，最多 boot_max_ms
- boot 完成后若已过最短时间 → 立即关闭
- 到达最长时间仍未完 → 强制关闭（boot 继续在后台跑）
- 关闭后回调 on_done
- 进度条单向推进，到顶后停住
- 无点击跳过，窗口不可拖动
- logo 宽度与进度条对齐
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ..core import database as db
from ..theme import Color, Font

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOGO_PATH = _PROJECT_ROOT / "assets" / "Pulses0-new.png"

_CHROMA_KEY = "#010203"


class SplashScreen(ctk.CTkToplevel):
    def __init__(self, master,
                 on_done: Callable[[], None] | None = None,
                 boot_tasks: list | None = None):
        super().__init__(master)
        self._on_done = on_done
        self._closed = False
        self._boot_started = 0.0
        self._force_after_id: str | None = None
        self._close_after_id: str | None = None

        try:
            ui_opts = db.get_ui_options()
        except Exception:  # noqa: BLE001
            ui_opts = {}
        self._min_ms = int(ui_opts.get("boot_min_ms", 1500))
        self._max_ms = int(ui_opts.get("boot_max_ms", 3000))

        self._logo_image: ctk.CTkImage | None = None
        self._progress_value: float = 0.0

        # 进度条左右内边距（logo 与进度条等宽）
        self._pad_x = 30
        # 进度条与 logo 底部的重合量（px）
        self._overlap = 20

        try:
            sw = self.winfo_screenwidth()
        except Exception:  # noqa: BLE001
            sw = 1280
        w = int(sw * 0.5)
        w = max(500, min(1000, w))
        self._win_w = w

        # 预先算好相对位置（place 不允许传 width/height，只能用 rel*）
        self._inner_relx = self._pad_x / self._win_w
        self._inner_relw = (self._win_w - self._pad_x * 2) / self._win_w

        self._build()
        self._schedule_boot(boot_tasks or [])

    # ------------------------------------------------------------------
    def _build(self):
        try:
            self.overrideredirect(True)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] overrideredirect 失败：{e}")

        self.configure(fg_color=_CHROMA_KEY)
        try:
            self.attributes("-transparentcolor", _CHROMA_KEY)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] transparentcolor 设置失败：{e}")

        try:
            self.attributes("-topmost", True)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] topmost 设置失败：{e}")

        # 先给一个临时几何
        self.geometry(f"{self._win_w}x100+0+0")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=_CHROMA_KEY,
                            corner_radius=0)
        card.grid(row=0, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        self._card = card
        card.grid_propagate(False)

        # ---- logo 容器（place，relx/relwidth 控制左右留白） ----
        self._logo_wrap = ctk.CTkFrame(card, fg_color=_CHROMA_KEY,
                                       corner_radius=0)
        self._logo_wrap.place(x=self._pad_x, y=24,
                              relx=self._inner_relx,
                              relwidth=self._inner_relw)
        self._logo_wrap.grid_columnconfigure(0, weight=1)

        # ---- 进度条 ----
        self.progress = ctk.CTkProgressBar(
            card, height=4, corner_radius=2,
            fg_color=Color.BORDER, progress_color=Color.ACCENT)
        self.progress.set(0)

        # 窗口映射完成后再加载 logo
        self.after(30, self._load_and_place_logo)

        self._animate_progress()

    # ------------------------------------------------------------------
    def _load_and_place_logo(self):
        """窗口映射后：按真实宽度加载 logo，动态调整窗口高度和子控件位置"""
        logo = self._build_logo_image()
        logo_h = 0
        if logo is None:
            lbl = ctk.CTkLabel(self._logo_wrap, text="Pulses Easier",
                               font=(Font.FAMILY, 28, "bold"),
                               text_color=Color.TEXT_PRIMARY,
                               fg_color=_CHROMA_KEY)
            lbl.grid(row=0, column=0)
            logo_h = 60
        else:
            self._logo_image = logo
            logo_label = ctk.CTkLabel(self._logo_wrap, text="",
                                      image=logo,
                                      fg_color=_CHROMA_KEY)
            logo_label.grid(row=0, column=0)
            try:
                logo_h = logo._size[1]  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logo_h = 120

        # 布局计算：logo → 进度条
        top_pad = 24
        logo_bottom = top_pad + logo_h
        progress_h = 4
        progress_y = max(top_pad, logo_bottom - self._overlap)
        bottom_pad = 24
        total_h = progress_y + progress_h + bottom_pad

        # logo_wrap 高度调整（place 不允许传 height，改用 place_configure 也不允许，
        # 用 grid_propagate + 内部 layout 决定高度即可，此处不必显式设高）

        # 进度条（lift 到最上层）
        try:
            self.progress.place(x=self._pad_x, y=progress_y,
                                relx=self._inner_relx,
                                relwidth=self._inner_relw)
            self.progress.lift()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度条定位失败：{e}")

        self._resize_to_content(total_h)

    def _resize_to_content(self, content_h: int):
        try:
            sh = self.winfo_screenheight()
        except Exception:  # noqa: BLE001
            sh = 800
        h = max(180, content_h)
        x = (self.winfo_screenwidth() - self._win_w) // 2
        y = (sh - h) // 2
        try:
            self.geometry(f"{self._win_w}x{h}+{x}+{y}")
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 几何调整失败：{e}")

    def _build_logo_image(self) -> ctk.CTkImage | None:
        """加载 logo：宽度 = 窗口宽度 - 2*pad_x，无高度上限"""
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            return None
        try:
            if not _LOGO_PATH.is_file():
                return None
            pil = Image.open(_LOGO_PATH).convert("RGBA")
            w, h = pil.size
            if w <= 0 or h <= 0:
                return None

            target_w = max(200, self._win_w - self._pad_x * 2)
            target_h = max(1, int(h * (target_w / w)))

            return ctk.CTkImage(light_image=pil, dark_image=pil,
                                size=(target_w, target_h))
        except Exception as e:  # noqa: BLE001
            print(f"[splash] logo 加载失败：{e}")
            return None

    # ------------------------------------------------------------------
    def _animate_progress(self):
        """单向推进；到顶后停住，不再回滚"""
        if self._closed:
            return
        if self._progress_value >= 1.0:
            return
        step = 1.0 / max(1.0, self._max_ms / 30.0)
        self._progress_value = min(1.0, self._progress_value + step)
        try:
            self.progress.set(self._progress_value)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度设置失败：{e}")
            return
        if self._progress_value >= 1.0:
            return
        try:
            self.after(30, self._animate_progress)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度动画调度失败：{e}")

    def _finish_progress(self):
        self._progress_value = 1.0
        try:
            self.progress.set(1.0)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度收尾失败：{e}")

    # ------------------------------------------------------------------
    def _schedule_boot(self, tasks: list):
        self._boot_started = time.time()
        self._force_after_id = self.after(self._max_ms, self._try_close)

        def _runner():
            for item in tasks:
                if self._closed:
                    return
                try:
                    name, fn = item
                except Exception as e:  # noqa: BLE001
                    print(f"[splash] 无效 boot 项：{e}")
                    continue
                self.after(0, lambda n=name: self._set_status(n))
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    print(f"[splash] boot 任务 {name} 失败：{e}")
            self.after(0, self._finish_progress)
            self.after(0, self._try_close)

        threading.Thread(target=_runner, daemon=True).start()

    def _set_status(self, text: str):
        # 状态文字已移除，保留方法以免调用处报错
        pass
    def _try_close(self):
        if self._closed:
            return
        elapsed_ms = (time.time() - self._boot_started) * 1000
        if elapsed_ms < self._min_ms:
            delay = int(self._min_ms - elapsed_ms)
            if self._close_after_id is not None:
                try:
                    self.after_cancel(self._close_after_id)
                except Exception as e:  # noqa: BLE001
                    print(f"[splash] after_cancel 失败：{e}")
            self._close_after_id = self.after(delay, self._do_close)
            return
        self._do_close()

    def _do_close(self):
        if self._closed:
            return
        self._closed = True
        for aid in (self._force_after_id, self._close_after_id):
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception as e:  # noqa: BLE001
                    print(f"[splash] 关闭前 after_cancel 失败：{e}")
        self._force_after_id = None
        self._close_after_id = None

        try:
            self.destroy()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] destroy 失败：{e}")

        if self._on_done is not None:
            try:
                self._on_done()
            except Exception as e:  # noqa: BLE001
                print(f"[splash] on_done 回调失败：{e}")
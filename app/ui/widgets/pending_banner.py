"""
pending_banner.py — 主窗口右栏的"更新受阻"提示条
================================================
子窗口可以随时收起，这个提示条负责让状态**一直可见**，并提供重开入口：

    ▌ ⚠ 更新受阻 · 还差 2 个文件
      [打开补入窗口]            [放弃…]

配色沿用主题里的警示色（低饱和），只做重点强调。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Color, Font, Size


class PendingBanner(ctk.CTkFrame):
    def __init__(self, master, *,
                 on_open: Callable[[], None] | None = None,
                 on_give_up: Callable[[], None] | None = None,
                 on_dismiss: Callable[[], None] | None = None,
                 mode: str = "wait",
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_open = on_open
        self.on_give_up = on_give_up
        self.on_dismiss = on_dismiss
        self.mode = mode
        self._count = 0

        self.grid_columnconfigure(1, weight=1)

        # 左侧警示色竖条（"任务受阻"的视觉标记）
        ctk.CTkFrame(self, fg_color=Color.WARNING, width=3,
                     corner_radius=0).grid(row=0, column=0, sticky="ns",
                                           rowspan=2)

        self.title_label = ctk.CTkLabel(
            self, text="⚠ 更新受阻", font=Font.BODY_B,
            text_color=Color.WARNING, fg_color=Color.TRANSPARENT,
            anchor="w")
        self.title_label.grid(row=0, column=1, sticky="w",
                              padx=(10, 6), pady=(10, 0))

        self.hint_label = ctk.CTkLabel(
            self, text="下不来的文件需要你自己下载后拖进去",
            font=Font.TINY, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.hint_label.grid(row=1, column=1, sticky="w",
                             padx=(10, 6), pady=(0, 10))

        btns = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        btns.grid(row=0, column=2, rowspan=2, sticky="e",
                  padx=(4, 10), pady=8)

        self.open_btn = ctk.CTkButton(
            btns, text="打开补入窗口", width=110, height=28,
            font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._fire(on_open))
        self.open_btn.pack(side="left", padx=(0, 4))

        if mode == "wait":
            self.alt_btn = ctk.CTkButton(
                btns, text="放弃…", width=64, height=28,
                font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.ERROR,
                command=self._fire(on_give_up))
        else:
            self.alt_btn = ctk.CTkButton(
                btns, text="不再提醒", width=84, height=28,
                font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.TEXT_SECONDARY,
                command=self._fire(on_dismiss))
        self.alt_btn.pack(side="left")

    @staticmethod
    def _fire(cb):
        def _inner():
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001, S110
                    pass
        return _inner

    def set_count(self, count: int, mode: str | None = None):
        self._count = max(0, int(count))
        if mode is not None:
            self.mode = mode
        try:
            if self.mode == "wait":
                self.title_label.configure(
                    text=f"⚠ 更新受阻 · 还差 {self._count} 个文件")
                self.hint_label.configure(
                    text="这些文件所有下载源都失败了：点源名称自己下，"
                         "再拖进补入窗口")
            else:
                self.title_label.configure(
                    text=f"⚠ 上次更新未完成 · 还差 {self._count} 个文件")
                self.hint_label.configure(
                    text="补齐后本次更新才算完整")
        except Exception:  # noqa: BLE001, S110
            pass

    def set_mode(self, mode: str):
        self.set_count(self._count, mode=mode)

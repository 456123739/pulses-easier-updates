"""
log_panel.py — 日志 / 警告区
------------------------------------------------
顶部标题行 + 右侧清空按钮。
清空时不直接删内容，而是回调给外部，由外部决定清空后填什么。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Color, Font, Size

_LEVEL_COLORS = {
    "info":  Color.TEXT_SECONDARY,
    "warn":  Color.WARNING,
    "error": Color.ERROR,
}


class LogPanel(ctk.CTkFrame):
    def __init__(self, master, height: int = Size.LOG_H,
                 title: str = "日志",
                 on_clear: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.LOG_BG,
                         corner_radius=Size.RADIUS_BUTTON, **kwargs)

        self.on_clear = on_clear
        # 日志上限（首选项 ui.log_max_lines）：超出后丢弃最早的行，
        # 否则长时间下载会把整个会话的日志都堆在 Tk 文本控件里
        self._max_lines = self._read_max_lines()
        self._since_limit_check = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 标题行
        head = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(head, text=title, font=Font.TINY,
                     text_color=Color.TEXT_MUTED,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, sticky="w")

        ctk.CTkButton(head, text="清空", width=48, height=20,
                      font=Font.TINY, corner_radius=6,
                      fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self._on_clear_click)\
            .grid(row=0, column=1, sticky="e")

        # 内容区
        self.text = ctk.CTkTextbox(
            self, fg_color=Color.LOG_BG, text_color=Color.TEXT_SECONDARY,
            font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
            border_width=0, wrap="word", height=height,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.text.grid(row=1, column=0, sticky="nsew",
                       padx=8, pady=(4, 8))
        self.text.configure(state="disabled")

        inner = self.text._textbox
        for level, color in _LEVEL_COLORS.items():
            inner.tag_config(level, foreground=color)

    @staticmethod
    def _read_max_lines() -> int:
        try:
            from ...core import database as db
            n = int(db.get_ui_options().get("log_max_lines", 500) or 500)
            return max(50, n)
        except Exception:  # noqa: BLE001
            return 500

    def set_max_lines(self, n: int):
        self._max_lines = max(50, int(n))
        self._trim(force=True)

    def _trim(self, force: bool = False):
        self._since_limit_check += 1
        if not force and self._since_limit_check < 32:
            return
        self._since_limit_check = 0
        if not force:
            # 周期性重读配置，用户改完首选项不用重启
            self._max_lines = self._read_max_lines()
        try:
            inner = self.text._textbox
            total = int(inner.index("end-1c").split(".")[0])
            over = total - self._max_lines
            if over > 0:
                inner.configure(state="normal")
                inner.delete("1.0", f"{over + 1}.0")
                inner.configure(state="disabled")
        except Exception:  # noqa: BLE001, S110
            pass

    def log(self, level: str, message: str):
        inner = self.text._textbox
        inner.configure(state="normal")
        inner.insert("end", message + "\n", level)
        inner.see("end")
        inner.configure(state="disabled")
        self._trim()

    def clear(self):
        inner = self.text._textbox
        inner.configure(state="normal")
        inner.delete("1.0", "end")
        inner.configure(state="disabled")

    def _on_clear_click(self):
        if self.on_clear:
            self.on_clear()
        else:
            self.clear()
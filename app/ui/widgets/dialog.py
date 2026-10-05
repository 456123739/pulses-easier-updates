"""
dialog.py — 自定义弹窗组件
------------------------------------------------
  AlertDialog      单按钮提示
  ConfirmDialog    两按钮确认
  LocateDialog     定位类：说明 + 「重新定位」+「取消继续更新」
  （可选）extra_checkbox：底部左侧复选框，如「不再提醒」

用法：
  alert(parent, "提示", "更新完成")
  confirm(parent, "确认", "是否关闭？", on_result=lambda ok: ...)
  LocateDialog(parent, "无法继续更新", "...",
               locate_label="重新定位整合包",
               on_located=lambda p: ..., on_cancel=lambda: ...)
  # 带复选框：
  confirm(parent, "继续上次更新？", "...",
          on_result=lambda ok: ...,
          extra_checkbox=("不再提醒", lambda v: ...))
"""

from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...theme import Color, Font, Size

_LEVEL_COLOR = {
    "info":    Color.ACCENT,
    "warn":    Color.WARNING,
    "error":   Color.ERROR,
    "success": Color.ADDED,
}
_LEVEL_ICON = {
    "info":    "ℹ",
    "warn":    "⚠",
    "error":   "✕",
    "success": "✓",
}


class _BaseDialog(ctk.CTkToplevel):
    def __init__(self, parent, title: str, message: str,
                 level: str = "info",
                 width: int = 480, height: int = 240,
                 extra_checkbox: tuple[str, Callable[[bool], None]]
                 | None = None):
        super().__init__(parent)

        self.title(title)
        self.geometry(f"{width}x{height}")
        self.configure(fg_color=Color.WINDOW_BG)
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        try:
            self.update_idletasks()
            px = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:  # noqa: BLE001, S110
            pass

        self._level = level
        self._on_close_cb: Callable[[], None] | None = None
        self._checkbox_var: ctk.BooleanVar | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header(title)
        self._build_body(message)
        self._build_footer(extra_checkbox)

        self.protocol("WM_DELETE_WINDOW", self._on_x_click)
        self.bind("<Escape>", lambda e: self._on_x_click())
        self.after(80, self._grab_focus)

    def _grab_focus(self):
        try:
            self.focus_force()
        except Exception:  # noqa: BLE001, S110
            pass

    def _build_header(self, title: str):
        header = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                              corner_radius=0, height=52)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        color = _LEVEL_COLOR.get(self._level, Color.ACCENT)
        icon = _LEVEL_ICON.get(self._level, "ℹ")

        bar = ctk.CTkFrame(header, fg_color=color, width=4,
                           corner_radius=0)
        bar.grid(row=0, column=0, sticky="ns", padx=(0, 0))
        bar.grid_propagate(False)

        ctk.CTkLabel(header, text=icon,
                     font=(Font.FAMILY, 18, "bold"),
                     text_color=color, fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=1, sticky="w", padx=(16, 8))

        ctk.CTkLabel(header, text=title,
                     font=(Font.FAMILY, 15, "bold"),
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=2, sticky="w")

        ctk.CTkButton(header, text="✕", width=32, height=32,
                      font=Font.BODY, corner_radius=6,
                      fg_color=Color.TRANSPARENT,
                      hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self._on_x_click)\
            .grid(row=0, column=3, sticky="e", padx=12)

    def _build_body(self, message: str):
        body = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                            corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        text_box = ctk.CTkTextbox(
            body, fg_color=Color.CARD_BG,
            text_color=Color.TEXT_PRIMARY,
            font=Font.BODY, corner_radius=0, border_width=0,
            wrap="word",
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        text_box.grid(row=0, column=0, sticky="nsew",
                      padx=24, pady=(4, 12))
        text_box.insert("1.0", message or "")
        text_box.configure(state="disabled")

    def _build_footer(self, extra_checkbox):
        self.footer = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                                   corner_radius=0, height=64)
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer.grid_propagate(False)

        # 左侧复选框（若提供）
        if extra_checkbox is not None:
            label, on_change = extra_checkbox
            try:
                self._checkbox_var = ctk.BooleanVar(value=False)
                cb = ctk.CTkCheckBox(
                    self.footer, text=label,
                    variable=self._checkbox_var,
                    font=Font.SMALL,
                    text_color=Color.TEXT_SECONDARY,
                    fg_color=Color.ACCENT,
                    hover_color=Color.ACCENT_HOVER,
                    checkmark_color=Color.TEXT_ON_ACCENT,
                    border_color=Color.BORDER,
                    corner_radius=4,
                    command=lambda: self._on_checkbox_change(on_change))
                cb.pack(side="left", padx=(24, 0), pady=14)
            except Exception:  # noqa: BLE001
                self._checkbox_var = None

    def _on_checkbox_change(self, on_change):
        try:
            val = bool(self._checkbox_var.get()
                       if self._checkbox_var is not None else False)
            on_change(val)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_x_click(self):
        if self._on_close_cb is not None:
            try:
                self._on_close_cb()
            except Exception:  # noqa: BLE001, S110
                pass
        self._safe_destroy()

    def _safe_destroy(self):
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001, S110
            pass


class AlertDialog(_BaseDialog):
    def __init__(self, parent, title: str, message: str,
                 level: str = "info",
                 button_text: str = "知道了",
                 on_close: Callable[[], None] | None = None):
        super().__init__(parent, title, message, level,
                         width=480, height=240)
        self._on_close_cb = on_close

        ctk.CTkButton(
            self.footer, text=button_text, width=100, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            command=self._on_ok)\
            .pack(side="right", padx=(8, 24), pady=14)

    def _on_ok(self):
        if self._on_close_cb is not None:
            try:
                self._on_close_cb()
            except Exception:  # noqa: BLE001, S110
                pass
        self._safe_destroy()


class ConfirmDialog(_BaseDialog):
    def __init__(self, parent, title: str, message: str,
                 confirm_text: str = "确定",
                 cancel_text: str = "取消",
                 level: str = "info",
                 danger: bool = False,
                 on_result: Callable[[bool], None] | None = None,
                 extra_checkbox: tuple[str, Callable[[bool], None]]
                 | None = None):
        super().__init__(parent, title, message, level,
                         width=480, height=250,
                         extra_checkbox=extra_checkbox)
        self._on_result = on_result
        self._result_sent = False
        self._on_close_cb = self._on_cancel

        confirm_color = (Color.ERROR if danger else Color.ACCENT)
        confirm_hover = ("#D97A7A" if danger else Color.ACCENT_HOVER)
        confirm_tx = (Color.TEXT_PRIMARY if danger
                      else Color.TEXT_ON_ACCENT)

        ctk.CTkButton(
            self.footer, text=confirm_text, width=110, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=confirm_color, hover_color=confirm_hover,
            text_color=confirm_tx,
            command=self._on_confirm)\
            .pack(side="right", padx=(8, 24), pady=14)

        ctk.CTkButton(
            self.footer, text=cancel_text, width=100, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_cancel)\
            .pack(side="right", padx=0, pady=14)

    def _emit(self, ok: bool):
        if self._result_sent:
            return
        self._result_sent = True
        if self._on_result is not None:
            try:
                self._on_result(ok)
            except Exception:  # noqa: BLE001, S110
                pass

    def _on_confirm(self):
        self._emit(True)
        self._safe_destroy()

    def _on_cancel(self):
        self._emit(False)
        self._safe_destroy()

    def _on_x_click(self):
        self._on_cancel()


class LocateDialog(_BaseDialog):
    """
    定位类弹窗：说明缺哪个 + 「重新定位」+「取消继续更新」。
    """

    def __init__(self, parent, title: str, message: str,
                 locate_label: str = "重新定位",
                 on_located: Callable[[Path], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 level: str = "warn",
                 dialog_title: str = "选择目录",
                 extra_checkbox: tuple[str, Callable[[bool], None]]
                 | None = None):
        super().__init__(parent, title, message, level,
                         width=520, height=270,
                         extra_checkbox=extra_checkbox)
        self._on_located = on_located
        self._on_cancel = on_cancel
        self._dialog_title = dialog_title

        ctk.CTkButton(
            self.footer, text=locate_label, width=140, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            command=self._on_locate)\
            .pack(side="right", padx=(8, 24), pady=14)

        ctk.CTkButton(
            self.footer, text="取消继续更新", width=120, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_cancel_click)\
            .pack(side="right", padx=0, pady=14)

        self._on_close_cb = self._on_cancel_click

    def _on_locate(self):
        try:
            folder = filedialog.askdirectory(
                title=self._dialog_title,
                parent=self)
        except Exception:  # noqa: BLE001
            folder = ""
        if not folder:
            return
        path = Path(folder)
        if self._on_located is not None:
            try:
                self._on_located(path)
            except Exception:  # noqa: BLE001, S110
                pass
        self._safe_destroy()

    def _on_cancel_click(self):
        if self._on_cancel is not None:
            try:
                self._on_cancel()
            except Exception:  # noqa: BLE001, S110
                pass
        self._safe_destroy()


# ----------------------------------------------------------------------
# 便捷函数
# ----------------------------------------------------------------------
def alert(parent, title: str, message: str,
          level: str = "info",
          on_close: Callable[[], None] | None = None) -> AlertDialog:
    return AlertDialog(parent, title, message, level, on_close=on_close)


def confirm(parent, title: str, message: str,
            confirm_text: str = "确定",
            cancel_text: str = "取消",
            level: str = "info",
            danger: bool = False,
            on_result: Callable[[bool], None] | None = None,
            extra_checkbox: tuple[str, Callable[[bool], None]]
            | None = None) -> ConfirmDialog:
    return ConfirmDialog(parent, title, message,
                         confirm_text=confirm_text,
                         cancel_text=cancel_text,
                         level=level, danger=danger,
                         on_result=on_result,
                         extra_checkbox=extra_checkbox)
"""
drop_zone.py — 可复用拖入框
------------------------------------------------
两层 Frame 嵌套实现描边：
  - 外层 fg_color = 描边色（青色 = 引导态，深灰 = 普通态）
  - 内层 fg_color = 背景色，grid 时留 2px 边距
颜色切换通过 set_highlight() 修改外层 fg_color。
"""

import re
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...theme import Anim, Color, Font, Size
from ...utils.animation import animate_color

try:
    from tkinterdnd2 import DND_FILES
    _DND_READY = True
except Exception:
    DND_FILES = None
    _DND_READY = False


class DropZone(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str = "",
                 height: int = Size.DROP_H_WORK,
                 on_drop: Callable[[list[Path]], None] | None = None,
                 mode: str = "any",
                 highlight: bool = False,
                 **kwargs):
        super().__init__(master,
                         fg_color=Color.ACCENT if highlight else Color.LOG_BG,
                         corner_radius=Size.RADIUS_DROP,
                         height=height, **kwargs)
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.on_drop = on_drop
        self.mode = mode
        self._height = height
        self._registered = False
        self._subtitle_base = subtitle
        self._highlight = highlight

        # 内层：真正的拖入区，grid 时留 2px 边距露出外层描边
        self._inner = ctk.CTkFrame(
            self,
            fg_color=Color.LOG_BG,
            corner_radius=Size.RADIUS_DROP - 2)
        self._inner.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        self._inner.grid_propagate(False)
        self._inner.grid_rowconfigure(0, weight=1)
        self._inner.grid_columnconfigure(0, weight=1)

        # 主 / 副标题（用 place 在 _inner 上居中）
        self.label_title = ctk.CTkLabel(
            self._inner, text=title, font=Font.BODY,
            text_color=Color.TEXT_PRIMARY,
            fg_color=Color.TRANSPARENT)
        self.label_title.place(relx=0.5, rely=0.42, anchor="center")

        self.label_sub = ctk.CTkLabel(
            self._inner, text=subtitle, font=Font.SMALL,
            text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT)
        if subtitle:
            self.label_sub.place(relx=0.5, rely=0.64, anchor="center")

        # 绑定悬停 + 点击
        for w in (self._inner, self.label_title, self.label_sub):
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
            w.bind("<Button-1>", self._on_click)

        self.after(60, self._register_dnd)

    # ------------------------------------------------------------------
    def _register_dnd(self):
        if not _DND_READY or self._registered:
            return
        try:
            for w in (self._inner, self.label_title, self.label_sub):
                register = getattr(w, "drop_target_register", None)
                bind = getattr(w, "dnd_bind", None)
                if register is None or bind is None:
                    raise AttributeError("DnD 方法未注入")
                register(DND_FILES)
                bind("<<Drop>>", self._handle_drop)
                bind("<<DropEnter>>", lambda e: self._hover(True))
                bind("<<DropLeave>>", lambda e: self._hover(False))
            self._registered = True
            self._update_subtitle_hint()
        except Exception:  # noqa: BLE001
            self._registered = False

    def _update_subtitle_hint(self):
        hint = "或点击选择"
        if self._subtitle_base:
            text = f"{self._subtitle_base} · {hint}"
        else:
            text = hint
        try:
            self.label_sub.configure(text=text)
            self.label_sub.place(relx=0.5, rely=0.64, anchor="center")
        except Exception:  # noqa: BLE001, S110
            pass

    def _handle_drop(self, event):
        try:
            paths = self._parse_drop(event.data)
        except Exception:  # noqa: BLE001
            paths = []
        if paths:
            self._dispatch(paths)

    @staticmethod
    def _parse_drop(data: str) -> list[Path]:
        if not data:
            return []
        items = re.findall(r"\{([^}]*)\}|(\S+)", data)
        result: list[Path] = []
        for braced, plain in items:
            raw = braced or plain
            if raw:
                result.append(Path(raw))
        return result

    # ------------------------------------------------------------------
    def _on_click(self, _event=None):
        try:
            if self.mode == "zip":
                path = filedialog.askopenfilename(
                    title="选择更新包",
                    filetypes=[
                        ("整合包 / 更新包", "*.zip *.eapack *.mrpack"),
                        ("ZIP 文件", "*.zip"),
                        ("Pulses Easier 更新包", "*.eapack"),
                        ("Modrinth 整合包", "*.mrpack"),
                        ("所有文件", "*.*"),
                    ])
            elif self.mode == "dir":
                path = filedialog.askdirectory(title="选择文件夹")
            else:
                path = filedialog.askopenfilename(title="选择文件")
        except Exception:  # noqa: BLE001
            return
        if path:
            self._dispatch([Path(path)])

    def _dispatch(self, paths: list[Path]):
        if self.on_drop:
            self.on_drop(paths)

    # ------------------------------------------------------------------
    def _hover(self, entering: bool):
        """悬停/拖入时内层背景色过渡"""
        try:
            start = self._inner.cget("fg_color")
        except Exception:  # noqa: BLE001
            return
        end = Color.CARD_BG if entering else Color.LOG_BG
        try:
            animate_color(self._inner, "fg_color", start, end,
                          duration_ms=Anim.DROP_MS)
        except Exception:  # noqa: BLE001
            try:
                self._inner.configure(fg_color=end)
            except Exception:  # noqa: BLE001, S110
                pass

    # ------------------------------------------------------------------
    def set_highlight(self, on: bool):
        """
        切换引导态：
          - on=True：外层描边为青色
          - on=False：外层描边为深灰
        """
        self._highlight = on
        try:
            self.configure(
                fg_color=Color.ACCENT if on else Color.LOG_BG)
        except Exception:  # noqa: BLE001, S110
            pass

    def set_text(self, title: str | None = None,
                 subtitle: str | None = None):
        """更新提示文字"""
        if title is not None:
            try:
                self.label_title.configure(text=title,
                                           text_color=Color.TEXT_PRIMARY)
            except Exception:  # noqa: BLE001, S110
                pass
        if subtitle is not None:
            self._subtitle_base = subtitle
            if subtitle:
                try:
                    self.label_sub.configure(text=subtitle)
                    self.label_sub.place(relx=0.5, rely=0.64, anchor="center")
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                try:
                    self.label_sub.place_forget()
                except Exception:  # noqa: BLE001, S110
                    pass

    def set_step_hint(self, step_text: str, subtitle: str = ""):
        """第一行加 step 标签的便捷方法"""
        try:
            self.label_title.configure(text=step_text)
            self._subtitle_base = subtitle
            if subtitle:
                self.label_sub.configure(text=subtitle)
                self.label_sub.place(relx=0.5, rely=0.64, anchor="center")
            else:
                self.label_sub.place_forget()
        except Exception:  # noqa: BLE001, S110
            pass
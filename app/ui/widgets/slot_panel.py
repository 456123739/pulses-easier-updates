"""
slot_panel.py — 并行槽位展开面板
------------------------------------------------
标题栏带「收起」按钮，点击回调 on_toggle(False)。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Color, Font, Size


def _shorten(path: str, max_len: int = 36) -> str:
    if not path:
        return ""
    if len(path) <= max_len:
        return path
    half = (max_len - 3) // 2
    return path[:half] + "…" + path[-half:]


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "0 B/s"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    v = float(bps)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B/s" else f"{int(v)} {u}"
        v /= 1024
    return f"{v:.1f} GB/s"


class _SlotRow(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, **kwargs)
        self.grid_columnconfigure(1, weight=1)

        self.name_label = ctk.CTkLabel(
            self, text="空闲", font=Font.TINY,
            text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w", width=200)
        self.name_label.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.bar = ctk.CTkProgressBar(
            self, height=4, corner_radius=2,
            fg_color=Color.BORDER, progress_color=Color.ACCENT)
        self.bar.grid(row=0, column=1, sticky="ew")
        self.bar.set(0)

        self.speed_label = ctk.CTkLabel(
            self, text="", font=Font.TINY,
            text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="e", width=80)
        self.speed_label.grid(row=0, column=2, sticky="e", padx=(8, 0))

    def set_idle(self):
        try:
            self.name_label.configure(text="空闲", text_color=Color.TEXT_MUTED)
            self.bar.set(0)
            self.speed_label.configure(text="")
        except Exception:
            pass

    def set_active(self, rel: str, progress: float, speed_bps: float):
        try:
            self.name_label.configure(text=_shorten(rel),
                                      text_color=Color.TEXT_PRIMARY)
            self.bar.set(max(0.0, min(1.0, progress)))
            self.speed_label.configure(text=_fmt_speed(speed_bps))
        except Exception:
            pass


class SlotPanel(ctk.CTkFrame):
    def __init__(self, master, slots: int = 12,
                 on_toggle: Callable[[bool], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)
        self._on_toggle = on_toggle
        self._expanded = False
        self._rows: list[_SlotRow] = []

        self.grid_columnconfigure(0, weight=1)

        # 标题栏 + 收起按钮
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="并行槽位", font=Font.SMALL,
                     text_color=Color.TEXT_SECONDARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, sticky="w")

        ctk.CTkButton(header, text="收起", width=60, height=24,
                      font=Font.TINY, corner_radius=6,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_PRIMARY,
                      command=self._on_collapse_click)\
            .grid(row=0, column=1, sticky="e")

        for i in range(max(1, slots)):
            row = _SlotRow(self)
            row.grid(row=i + 1, column=0, sticky="ew", padx=12, pady=(2, 2))
            self._rows.append(row)

        self.grid_remove()

    def _on_collapse_click(self):
        if self._on_toggle is not None:
            try:
                self._on_toggle(False)
            except Exception:
                pass

    def set_slots(self, n: int):
        if n == len(self._rows):
            return
        for r in self._rows:
            r.destroy()
        self._rows.clear()
        for i in range(max(1, n)):
            row = _SlotRow(self)
            row.grid(row=i + 1, column=0, sticky="ew", padx=12, pady=(2, 2))
            self._rows.append(row)

    def slot_count(self) -> int:
        return len(self._rows)

    def update(self, files: list[dict | None]):
        for i, row in enumerate(self._rows):
            item = files[i] if i < len(files) else None
            if item is None:
                row.set_idle()
            else:
                row.set_active(
                    item.get("rel", ""),
                    float(item.get("progress", 0.0)),
                    float(item.get("speed_bps", 0.0)))
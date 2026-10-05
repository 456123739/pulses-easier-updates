"""
progress_bar.py — 双层叠加进度条
------------------------------------------------
底层：整体进度（文件数），青色 ACCENT，高 6px
上层：当前文件内进度，暖黄 MODIFIED，高 3px，居中叠加
用 tk.Canvas 自绘圆角矩形，避免 CTkProgressBar 嵌套的宽度请求
导致父容器被撑大。

对外接口：
  set(value, animate=True)      整体进度 0~1
  set_file_progress(ratio)      文件内进度 0~1；None 隐藏上层
  set_text(text)                设置下方小字
  get_text() -> str
"""

import tkinter as tk

import customtkinter as ctk

from ...theme import Anim, Color


class SmoothProgressBar(ctk.CTkFrame):
    # 双层条的总高度（外层 Frame）
    _TOTAL_H = 12
    _BASE_H = 6          # 底层条高
    _OVERLAY_H = 3       # 上层条高
    _RADIUS = 3          # 圆角半径

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT,
                         corner_radius=0, height=self._TOTAL_H, **kwargs)
        # 阻止内部 Canvas 请求宽度影响父容器
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._value = 0.0
        self._file_ratio: float | None = None

        # 自绘 Canvas：宽度 1px 占位，实际宽度由 <Configure> 事件同步
        self._canvas = tk.Canvas(
            self, height=self._TOTAL_H, width=1,
            bg=Color.WORKSPACE_BG, highlightthickness=0, bd=0)
        self._canvas.pack(fill="x", expand=True)

        self._canvas.bind("<Configure>", self._on_canvas_resize)

    # ------------------------------------------------------------------
    def _on_canvas_resize(self, _event=None):
        self._redraw()

    def _redraw(self):
        c = self._canvas
        try:
            w = max(1, c.winfo_width())
            h = self._TOTAL_H
        except Exception:  # noqa: BLE001
            return

        try:
            c.delete("all")
        except Exception:  # noqa: BLE001
            return

        bg = Color.BORDER

        # 底层轨道（整条）
        self._round_rect(c, 0, (h - self._BASE_H) // 2,
                         w, (h - self._BASE_H) // 2 + self._BASE_H,
                         self._RADIUS, fill=bg)

        # 底层已填充部分
        fill_w = int(w * max(0.0, min(1.0, self._value)))
        if fill_w > 0:
            self._round_rect(c, 0, (h - self._BASE_H) // 2,
                             fill_w, (h - self._BASE_H) // 2 + self._BASE_H,
                             self._RADIUS, fill=Color.ACCENT)

        # 上层：文件内进度（居中叠加，更细，暖黄）
        if self._file_ratio is not None:
            r = max(0.0, min(1.0, self._file_ratio))
            fw = int(w * r)
            if fw > 0:
                y1 = (h - self._OVERLAY_H) // 2
                y2 = y1 + self._OVERLAY_H
                self._round_rect(c, 0, y1, fw, y2,
                                 max(1, self._OVERLAY_H // 2),
                                 fill=Color.MODIFIED)

    @staticmethod
    def _round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
        """在 Canvas 上画圆角矩形"""
        r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set(self, value: float, animate: bool = True):
        """整体进度 0~1"""
        value = max(0.0, min(1.0, value))
        if not animate:
            self._value = value
            self._redraw()
            return
        self._animate_value(self._value, value)

    def _animate_value(self, start: float, end: float,
                       duration_ms: int = 200):
        steps = max(1, duration_ms // Anim.FRAME_MS)

        def _step(i: int):
            t = i / steps
            self._value = start + (end - start) * t
            self._redraw()
            if i < steps:
                try:
                    self.after(Anim.FRAME_MS, _step, i + 1)
                except Exception:  # noqa: BLE001
                    return

        _step(0)

    def set_file_progress(self, ratio: float | None):
        """文件内进度 0~1；None 隐藏上层"""
        if ratio is None:
            self._file_ratio = None
        else:
            self._file_ratio = max(0.0, min(1.0, ratio))
        self._redraw()

    def get_file_progress(self) -> float | None:
        return self._file_ratio
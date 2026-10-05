"""
info_card.py — 整合包信息卡片
------------------------------------------------
显示图标、名称、版本、路径，底部有"清空"按钮。
背景 #333337，圆角 12px。

图标处理：
  - 按图片自身比例缩放到高度 48 以内。
  - 圆角裁剪贴着图片边缘，不额外加透明边。
  - Canvas 尺寸 = 图片缩放后尺寸，避免透明区渲染成黑边。

注意：本模块为包内模块，请通过 main.py 启动。
"""

import tkinter as tk
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...core.pack_info import ModpackInfo
from ...theme import Color, Font, Size

# 图标最大显示高度；宽度按比例
_ICON_MAX_H = 48
_ICON_MAX_W = 96   # 超宽图的上限，避免横幅图撑破卡片
_ICON_RADIUS = 10


def _pick_resample():
    """
    返回 Pillow 的重采样常量。
    Pillow 10+ 使用 Image.Resampling.LANCZOS；
    旧版回退到 Image.LANCZOS。
    """
    try:
        from PIL.Image import Resampling
        return Resampling.LANCZOS
    except ImportError:
        from PIL import Image
        return Image.LANCZOS  # type: ignore[attr-defined]


class InfoCard(ctk.CTkFrame):
    def __init__(self, master,
                 on_clear: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_clear = on_clear
        self._icon_photo = None
        self._full_path: str = ""
        self._short_path: str = ""

        self.grid_columnconfigure(1, weight=1)

        # 图标 Canvas：尺寸在 _draw_rounded_icon 里动态改
        self.icon_canvas = tk.Canvas(
            self, width=_ICON_MAX_H, height=_ICON_MAX_H,
            bg=Color.CARD_BG, highlightthickness=0, bd=0)
        self.icon_canvas.grid(row=0, column=0, padx=(12, 10),
                              pady=(12, 6), sticky="n")
        self._draw_placeholder()

        # 名称
        self.name_label = ctk.CTkLabel(
            self, text="未命名", font=(Font.FAMILY, 14, "bold"),
            text_color=Color.TEXT_PRIMARY,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.name_label.grid(row=0, column=1, sticky="ew",
                             padx=(0, 12), pady=(14, 0))

        # 版本
        self.version_label = ctk.CTkLabel(
            self, text="版本未知", font=Font.SMALL,
            text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.version_label.grid(row=1, column=1, sticky="ew",
                                padx=(0, 12), pady=(0, 4))

        # 路径
        self.path_label = ctk.CTkLabel(
            self, text="", font=Font.TINY,
            text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w",
            justify="left",
            wraplength=200)
        self.path_label.grid(row=2, column=0, columnspan=2,
                             sticky="ew", padx=12, pady=(0, 8))

        # 清空按钮
        self.clear_btn = ctk.CTkButton(
            self, text="清空", height=32,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._on_clear_click)
        self.clear_btn.grid(row=3, column=0, columnspan=2,
                            sticky="ew", padx=12, pady=(0, 12))

    # ------------------------------------------------------------------
    def set_info(self, info: ModpackInfo):
        """载入信息"""
        self.name_label.configure(text=info.name or "未命名")
        self.version_label.configure(text=f"版本 {info.version or '未知'}")

        parts = info.path.parts
        self._full_path = str(info.path)
        self._short_path = (str(Path(*parts[-2:])) if len(parts) >= 2
                            else str(info.path))
        self.path_label.configure(text=self._short_path)
        self.path_label.bind("<Enter>", self._show_full_path_tip)
        self.path_label.bind("<Leave>", self._hide_tip)

        self._apply_icon(info.icon_path)

    # ------------------------------------------------------------------
    def _apply_icon(self, icon_path: Path | None):
        """圆角裁剪并显示图标"""
        if icon_path and icon_path.is_file():
            try:
                self._draw_rounded_icon(icon_path)
                return
            except Exception:  # noqa: BLE001, S110
                pass

        self._draw_placeholder()

    def _draw_rounded_icon(self, icon_path: Path):
        """
        打开图片 → 按比例缩放 → 圆角裁剪 → 画到 Canvas。
        Canvas 尺寸随图片变化，不额外加透明边框。
        """
        from PIL import Image, ImageDraw, ImageTk

        pil = Image.open(icon_path).convert("RGBA")
        w, h = pil.size
        if w <= 0 or h <= 0:
            raise ValueError("图片尺寸异常")

        # 缩放：优先满足高度上限，宽度超限再按宽度缩
        scale = _ICON_MAX_H / h
        new_w = int(w * scale)
        new_h = _ICON_MAX_H
        if new_w > _ICON_MAX_W:
            scale = _ICON_MAX_W / new_w
            new_w = _ICON_MAX_W
            new_h = max(1, int(new_h * scale))

        resample = _pick_resample()
        pil = pil.resize((new_w, new_h), resample)

        # 圆角遮罩（按实际尺寸）
        radius = min(_ICON_RADIUS, new_h // 2, new_w // 2)
        mask = Image.new("L", (new_w, new_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, new_w - 1, new_h - 1),
            radius=radius, fill=255)
        pil.putalpha(mask)

        self._icon_photo = ImageTk.PhotoImage(pil)

        # Canvas 尺寸 = 图片尺寸
        self.icon_canvas.configure(width=new_w, height=new_h)
        self.icon_canvas.delete("all")
        self.icon_canvas.create_image(0, 0, anchor="nw",
                                      image=self._icon_photo)

    def _draw_placeholder(self):
        """无图标时：固定 48x48 圆角矩形 + emoji"""
        self._icon_photo = None
        size = _ICON_MAX_H
        self.icon_canvas.configure(width=size, height=size)
        self.icon_canvas.delete("all")
        self._round_rect(0, 0, size, size,
                         _ICON_RADIUS, fill=Color.LOG_BG)
        self.icon_canvas.create_text(
            size // 2, size // 2,
            text="📦", fill=Color.TEXT_SECONDARY,
            font=(Font.FAMILY, 20))

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        """在 Canvas 上画圆角矩形"""
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
        return self.icon_canvas.create_polygon(
            points, smooth=True, **kwargs)

    # ------------------------------------------------------------------
    def set_locked(self, locked: bool):
        try:
            if locked:
                self.clear_btn.configure(
                    state="disabled",
                    fg_color=Color.LOG_BG,
                    text_color=Color.TEXT_MUTED,
                    hover_color=Color.LOG_BG)
            else:
                self.clear_btn.configure(
                    state="normal",
                    fg_color=Color.LOG_BG,
                    text_color=Color.TEXT_SECONDARY,
                    hover_color=Color.BORDER)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _on_clear_click(self):
        if self.on_clear:
            self.on_clear()

    def _show_full_path_tip(self, _event=None):
        try:
            self.path_label.configure(text=self._full_path)
        except Exception:  # noqa: BLE001, S110
            pass

    def _hide_tip(self, _event=None):
        try:
            self.path_label.configure(text=self._short_path)
        except Exception:  # noqa: BLE001, S110
            pass
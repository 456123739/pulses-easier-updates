"""
transition.py — 换页过渡：logo 覆盖层（游戏切场景那种）
======================================================
玩家 ↔ 开发者换页时：

    1) 一块**渐变色**覆盖层从右侧滑入，盖住整个工作区（约 150ms）
    2) 覆盖层中央浮出 logo（小、居中、四周留白），轻轻放大一点
    3) logo 先淡出，然后覆盖层从中间上下对开（像大幕拉开），
       露出**已经建好**的新页面（约 170ms）

为什么这么做：Tk 里移动活的控件树每帧都会重绘子窗口，Text / 圆角控件会拉丝；
截图方案又会被压在页面上的窗口"拍进去"。这里全程只有
**两块纯色/渐变面板 + 一张 logo 图** 在动，页面上没有任何控件位移。

配色跟主窗口一致：`#1F1F24 → #2B2B32` 的竖向渐变，中间一层极淡的径向光晕
（logo 后面），既不抢眼也不像"一块死黑"。渐变是用 PIL 预渲染的位图，
按窗口尺寸缓存，换页时零额外开销。

Tk 没有控件级透明度：logo 的淡入淡出是预渲染几档 (alpha, scale) 换图，
每一档都把 logo **合成在对应位置的渐变底图上**，所以看不到方块背景。
没有 PIL / 没有 logo 资源时自动退化成纯色面板版本。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

from ..paths import asset
from ..utils.animation import ease_out_cubic, ease_out_quart, tween

# ---- 覆盖层配色（与主窗口同族，只是略深、带一点上层渐变）----
_TOP_RGB = (0x1F, 0x1F, 0x24)
_BOTTOM_RGB = (0x2B, 0x2B, 0x32)
_GLOW_RGB = (0x33, 0x33, 0x3C)      # 中央光晕的提亮色
_GLOW_STRENGTH = 52                 # 光晕峰值（0~255），很克制
_GLOW_RADIUS = 0.55                 # 相对短边

_LOGO_HEIGHT = 64                   # 小一点、留白多，才显得克制
_STUDIO_LINE = "PULSES 0 Studio  ·  NimShade & DS"
_TEXT_SIZE = 12                     # 小字：能看清但不抢 logo
_TEXT_GAP = 18                      # logo 与小字的间距（留白）
_TEXT_COLOR = (0x9C, 0x9C, 0xA8)
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_LOGO_STAGES = (
    (0.00, 0.94), (0.12, 0.955), (0.26, 0.968), (0.44, 0.978),
    (0.62, 0.986), (0.78, 0.992), (0.90, 0.996), (1.00, 1.00),
)

_COVER_CACHE: dict[tuple[int, int], object] = {}
_LOGO_CACHE: dict[tuple, list] = {}


# ----------------------------------------------------------------------
# 覆盖层底图（渐变 + 中央光晕）
# ----------------------------------------------------------------------
def _cover_image(w: int, h: int):
    key = (w, h)
    img = _COVER_CACHE.get(key)
    if img is not None:
        return img
    try:
        from PIL import Image, ImageDraw

        strip = Image.new("RGB", (1, h))
        for y in range(h):
            t = y / max(1, h - 1)
            strip.putpixel((0, y), (
                int(_TOP_RGB[0] + (_BOTTOM_RGB[0] - _TOP_RGB[0]) * t),
                int(_TOP_RGB[1] + (_BOTTOM_RGB[1] - _TOP_RGB[1]) * t),
                int(_TOP_RGB[2] + (_BOTTOM_RGB[2] - _TOP_RGB[2]) * t),
            ))
        img = strip.resize((max(1, w), max(1, h)))

        # 中央一层极淡的径向光晕：logo 后面略微提亮，避免"死黑一片"
        radius = max(2, int(min(w, h) * _GLOW_RADIUS))
        if radius >= 4:
            mask = Image.new("L", (img.width, img.height), 0)
            draw = ImageDraw.Draw(mask)
            cx, cy = img.width // 2, img.height // 2
            step = max(2, radius // 48)
            for r in range(radius, 0, -step):
                a = int(_GLOW_STRENGTH * (1.0 - r / radius) ** 1.7)
                draw.ellipse((cx - r, cy - int(r * 0.78),
                              cx + r, cy + int(r * 0.78)), fill=a)
            glow = Image.new("RGB", img.size, _GLOW_RGB)
            img = Image.composite(glow, img, mask).convert("RGB")
    except Exception:  # noqa: BLE001
        try:
            from PIL import Image as _I
            img = _I.new("RGB", (max(1, w), max(1, h)), _TOP_RGB)
        except Exception:  # noqa: BLE001
            return None
    _COVER_CACHE[key] = img
    return img


def _cover_color() -> str:
    return "#%02X%02X%02X" % _TOP_RGB


# ----------------------------------------------------------------------
# logo 各档帧（合成在渐变底图上，所以看不见方块背景）
# ----------------------------------------------------------------------
def _load_font(size: int):
    try:
        from PIL import ImageFont
        for path in _FONT_CANDIDATES:
            if Path(path).is_file():
                return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001
        pass
    return None


def _logo_frames(master, cover, w: int, h: int) -> list:
    key = (id(master), w, h, bool(cover))
    cached = _LOGO_CACHE.get(key)
    if cached is not None:
        return cached
    frames: list = []
    try:
        from PIL import Image, ImageTk

        path = Path(asset("logo.png"))
        if not path.is_file():
            if cover is not None:
                _LOGO_CACHE.clear()          # 换尺寸：旧的底图作废
            return []
        from PIL import ImageDraw

        src = Image.open(path).convert("RGBA")
        h0 = _LOGO_HEIGHT
        w0 = max(1, int(round(src.width * h0 / src.height)))
        base = src.resize((w0, h0), Image.LANCZOS)
        alpha0 = base.getchannel("A")
        cx, cy = w // 2, h // 2

        # 小字：工作室 + 作者（写在 logo 下方，一起淡入淡出）
        font = _load_font(_TEXT_SIZE)
        text_w = text_h = 0
        if font is not None:
            try:
                box = font.getbbox(_STUDIO_LINE)
                text_w = int(box[2] - box[0])
                text_h = int(box[3] - box[1])
            except Exception:  # noqa: BLE001
                text_w = text_h = 0

        # 一块 patch 同时容纳 logo 与小字：高度 = logo + 间距 + 小字
        pad_x = 18
        patch_w = max(w0, text_w) + pad_x * 2
        patch_h = h0 + (_TEXT_GAP + text_h if text_w else 0) + 10
        patch_left = cx - patch_w // 2
        patch_top = cy - h0 // 2 - 6

        for a, scale in _LOGO_STAGES:
            lw = max(1, int(round(w0 * scale)))
            lh = max(1, int(round(h0 * scale)))
            logo = base.resize((lw, lh), Image.LANCZOS)
            if a < 1.0:
                logo = logo.copy()
                logo.putalpha(alpha0.resize((lw, lh), Image.LANCZOS)
                              .point(lambda v, k=a: int(v * k)))
            if cover is not None:
                # 从同一张底图上裁一块，贴到 label 上 → 与周围渐变无缝
                patch = cover.crop((patch_left, patch_top,
                                    patch_left + patch_w,
                                    patch_top + patch_h)).convert("RGBA")
            else:
                patch = Image.new("RGBA", (patch_w, patch_h), _TOP_RGB + (255,))
            # logo 在 patch 里水平居中、贴着顶部留白
            patch.alpha_composite(
                logo, ((patch_w - lw) // 2, 6 + (lh - lh) // 2))
            if font is not None and text_w:
                color = _TEXT_COLOR + (int(255 * a),)
                draw = ImageDraw.Draw(patch)
                draw.text(((patch_w - text_w) // 2,
                           h0 + _TEXT_GAP + max(0, -text_h // 8)),
                          _STUDIO_LINE, font=font, fill=color)
            frames.append((ImageTk.PhotoImage(patch, master=master),
                           patch_w, patch_h))
    except Exception:  # noqa: BLE001
        frames = []
    _LOGO_CACHE.clear()                       # 只保留当前尺寸的一档
    _LOGO_CACHE[key] = frames
    return frames


# ----------------------------------------------------------------------
# 过渡本身
# ----------------------------------------------------------------------
def logo_transition(host, old_widget, new_widget, *,
                    duration_in: int = 150,
                    hold: int = 450,
                    duration_out: int = 170,
                    on_done=None) -> bool:
    """
    播放"logo 覆盖层"换页。返回 False 表示这个环境不支持（调用方退回落日方案）。
    `on_done` 一定会被调用一次（正常结束 / 宿主被销毁 / 出错兜底）。
    """
    try:
        w = int(host.winfo_width())
        h = int(host.winfo_height())
    except Exception:  # noqa: BLE001
        return False
    if w <= 16 or h <= 16:
        return False

    half = h // 2
    cover = _cover_image(w, h)
    frames = _logo_frames(host, cover, w, h)
    finished = {"v": False}
    logo = None

    def _finish():
        if finished["v"]:
            return
        finished["v"] = True
        for widget in (top, bottom, logo):
            try:
                widget.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    try:
        top = tk.Canvas(host, bd=0, highlightthickness=0,
                        bg=_cover_color())
        bottom = tk.Canvas(host, bd=0, highlightthickness=0,
                           bg=_cover_color())
        top.place(x=w, y=0, width=w, height=half)
        bottom.place(x=w, y=half, width=w, height=h - half)
        if cover is not None:
            try:
                from PIL import ImageTk
                top_img = ImageTk.PhotoImage(
                    cover.crop((0, 0, w, half)), master=host)
                bottom_img = ImageTk.PhotoImage(
                    cover.crop((0, half, w, h)), master=host)
                top.create_image(0, 0, anchor="nw", image=top_img)
                bottom.create_image(0, 0, anchor="nw", image=bottom_img)
                top._cover_img = top_img          # type: ignore[attr-defined]
                bottom._cover_img = bottom_img    # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110
                pass
        # Canvas 的 lift() 被 Tk 定义成"提升画布内 item"，提窗口要直接调 raise
        for panel in (top, bottom):
            panel.tk.call("raise", panel._w)
        for panel in (top, bottom):
            panel._transition_overlay = True      # type: ignore[attr-defined]

        if frames:
            photo, pw, ph = frames[0]
            logo = tk.Label(host, image=photo, bg=_cover_color(),
                            bd=0, highlightthickness=0)
            logo.place(x=(w - pw) // 2, y=max(0, h // 2 - ph // 2),
                       width=pw, height=ph)
            logo.lift()
            logo._transition_overlay = True       # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        for widget in (top, bottom):
            try:
                widget.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        return False

    logo_step = {"i": 0}

    def _set_logo(t: float):
        if logo is None or not frames:
            return
        idx = int(round(max(0.0, min(1.0, t)) * (len(frames) - 1)))
        if idx == logo_step["i"]:
            return
        logo_step["i"] = idx
        photo, pw, ph = frames[idx]
        try:
            logo.configure(image=photo)
            logo.place_configure(x=(w - pw) // 2,
                                 y=max(0, h // 2 - ph // 2),
                                 width=pw, height=ph)
        except Exception:  # noqa: BLE001, S110
            pass

    def _slide_in(t_eased: float, _raw: float):
        try:
            x = int(round(w * (1.0 - t_eased)))
            top.place_configure(x=x)
            bottom.place_configure(x=x)
        except Exception:  # noqa: BLE001, S110
            return
        if t_eased > 0.55:
            _set_logo((t_eased - 0.55) / 0.45)

    def _fade_logo_out():
        # 先把 logo 收掉再开幕，否则它会变成"页面上一个深色方块"
        if logo is None or not frames:
            _start_out()
            return

        def _frame(t_eased: float, _raw: float):
            _set_logo(1.0 - t_eased)

        def _done():
            try:
                logo.place_forget()
            except Exception:  # noqa: BLE001, S110
                pass
            _start_out()

        tween(host, max(40, int(duration_out * 0.40)), _frame, on_done=_done,
              ease=ease_out_cubic)

    def _start_out():
        # 换页发生在**完全盖住**的时候：玩家看不到任何控件在动
        try:
            new_widget.place(in_=host, relx=0, rely=0,
                             relwidth=1.0, relheight=1.0)
            old_widget.place_forget()
            new_widget.lift()
            for panel in (top, bottom):
                panel.tk.call("raise", panel._w)
        except Exception:  # noqa: BLE001, S110
            pass

        def _slide_out(t_eased: float, _raw: float):
            try:
                top.place_configure(y=int(round(-half * t_eased)))
                bottom.place_configure(
                    y=int(round(half + (h - half) * t_eased)))
            except Exception:  # noqa: BLE001, S110
                pass

        tween(host, duration_out, _slide_out, on_done=_finish,
              ease=ease_out_cubic)

    def _after_cover():
        try:
            host.after(max(0, int(hold)), _fade_logo_out)
        except Exception:  # noqa: BLE001
            _fade_logo_out()

    tween(host, duration_in, _slide_in, on_done=_after_cover,
          ease=ease_out_quart)
    return True


def clear_overlays(host) -> None:
    """清掉页面上残留的过渡覆盖层（换页被强行收尾时的兜底）。"""
    try:
        children = list(host.winfo_children())
    except Exception:  # noqa: BLE001
        return
    for child in children:
        if getattr(child, "_transition_overlay", False):
            try:
                child.destroy()
            except Exception:  # noqa: BLE001, S110
                pass

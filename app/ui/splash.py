"""
splash.py — 启动页
------------------------------------------------
独立无边框、透明背景窗口，居中显示 logo + 进度条。
- logo 淡入（先把窗口藏在屏幕外算好布局，再 alpha 0→1 渐显），不"啪"地弹出来
- 进度条居中放在 logo 卡片下方的留白里，宽度只有 logo 的 78% —— 不超出 logo
- 至少显示 boot_min_ms，最多 boot_max_ms
- boot 完成后 → 进度条平滑补满 → 关闭
- 到达最长时间仍未完 → 强制关闭（boot 继续在后台跑）
- 关闭后回调 on_done
- 伪进度：指数逼近但**永远不到顶**，带一点呼吸感；只有真的启动完成才补满
- 无点击跳过，窗口不可拖动
"""

import math
import threading
import time
from collections.abc import Callable

import customtkinter as ctk

from ..core import database as db
from ..paths import asset as _asset
from ..theme import Color, Font

_LOGO_PATH = _asset("Pulses0-new.png")

_CHROMA_KEY = "#010203"

# ---- 伪进度参数 ----
_PROGRESS_CAP = 0.90          # 启动期间最多走到这里，剩下的留给"真的完成了"
_PROGRESS_TAU_MS = 700.0      # 时间常数：越小前段越快、后面越慢
_PROGRESS_TICK_MS = 33        # ≈30fps
_PROGRESS_RIPPLE = 0.005      # 轻微呼吸（相对整条长度，约 ±2~3px）
_PROGRESS_RIPPLE_MS = 420.0   # 呼吸周期
_PROGRESS_RIPPLE_DECAY = 1500.0
_FINISH_MS = 260              # 收尾补满的时长
_FINISH_TICK_MS = 16

# ---- 淡入参数 ----
_FADE_MS = 420
_FADE_TICK_MS = 16

# ---- 进度条相对 logo 的宽度比例（居中，绝不超出 logo）----
_BAR_WIDTH_RATIO = 0.78


def ease_out_cubic(t: float) -> float:
    """0→1 的三次缓出。"""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def progress_at(elapsed_ms: float,
                cap: float = _PROGRESS_CAP,
                tau_ms: float = _PROGRESS_TAU_MS) -> float:
    """
    伪进度曲线：指数逼近 cap（先快后慢），叠一点逐渐衰减的呼吸。

    关键性质：**永远 < cap < 1.0** —— 到顶只可能来自 `_finish_progress()`，
    所以不会出现"还没启动完进度条就满了、然后干等着"。
    """
    if elapsed_ms <= 0:
        return 0.0
    base = cap * (1.0 - math.exp(-elapsed_ms / max(1.0, tau_ms)))
    ripple = (_PROGRESS_RIPPLE
              * math.sin(elapsed_ms / _PROGRESS_RIPPLE_MS * math.tau)
              * math.exp(-elapsed_ms / _PROGRESS_RIPPLE_DECAY))
    return max(0.0, min(cap, base + ripple))


def bar_geometry(logo_w: int, logo_x: int = 0,
                 ratio: float = _BAR_WIDTH_RATIO) -> tuple[int, int]:
    """进度条的水平位置与宽度：在 logo 内居中、比 logo 窄。返回 (x, w)。"""
    w = max(80, int(logo_w * ratio))
    x = logo_x + max(0, (logo_w - w) // 2)
    return x, w



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
        self._progress_after_id: str | None = None
        self._fade_after_id: str | None = None
        self._finish_after_id: str | None = None
        self._finishing = False
        self._alpha = 0.0

        try:
            ui_opts = db.get_ui_options()
        except Exception:  # noqa: BLE001
            ui_opts = {}
        self._min_ms = int(ui_opts.get("boot_min_ms", 1500))
        self._max_ms = int(ui_opts.get("boot_max_ms", 3000))

        self._logo_image: ctk.CTkImage | None = None
        self._progress_value: float = 0.0

        # logo 左右内边距（进度条另按 78% 居中，不再和 logo 等宽）
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

        # 先把窗口藏起来：logo 尺寸、进度条位置、窗口高度都要算完再露面，
        # 否则会先看到一个 100px 高的小窗在左上角闪一下再跳到屏幕中间。
        try:
            self.withdraw()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] withdraw 失败：{e}")

        self.configure(fg_color=_CHROMA_KEY)
        try:
            self.attributes("-transparentcolor", _CHROMA_KEY)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] transparentcolor 设置失败：{e}")

        # 淡入用：先全透明（不支持 -alpha 的平台会忽略，退化成直接显示）
        self._set_alpha(0.0)

        try:
            self.attributes("-topmost", True)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] topmost 设置失败：{e}")

        # 先给一个临时几何（此刻还藏着，玩家看不到）
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
        # 注意：x 与 relx 会**叠加**（Tk 的 place 语义）—— 两个都给会让
        # 偏移翻倍（logo 和进度条整体右移、右边顶到窗口边）。
        # 只用 relx/relwidth，绝对像素由 relx 换算。
        self._logo_wrap.place(y=24, relx=self._inner_relx,
                              relwidth=self._inner_relw)
        self._logo_wrap.grid_columnconfigure(0, weight=1)

        # ---- 进度条 ----
        self.progress = ctk.CTkProgressBar(
            card, height=4, corner_radius=2,
            fg_color=Color.BORDER, progress_color=Color.ACCENT)
        self.progress.set(0)

        # 窗口映射完成后再加载 logo（此时窗口还藏着）
        self.after(30, self._load_and_place_logo)

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

        # 进度条：居中、比 logo 窄（78%），所以不会从 logo 两边探出去。
        # 宽度按 logo 框宽算，换 logo（宽度变化）不用改这里。
        logo_x = self._pad_x
        logo_w = max(200, self._win_w - self._pad_x * 2)
        bar_x, bar_w = bar_geometry(logo_w, logo_x)
        try:
            self.progress.place(y=progress_y,
                                relx=bar_x / self._win_w,
                                relwidth=bar_w / self._win_w)
            self.progress.lift()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度条定位失败：{e}")

        self._resize_to_content(total_h)
        self._fade_in()

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
    def _set_progress(self, value: float):
        self._progress_value = value
        try:
            self.progress.set(value)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度设置失败：{e}")

    def _animate_progress(self):
        """伪进度：先快后慢地逼近 `_PROGRESS_CAP`，永远不到顶。"""
        if self._closed or self._finishing:
            return
        elapsed_ms = (time.time() - self._boot_started) * 1000.0
        self._set_progress(progress_at(elapsed_ms))
        try:
            self._progress_after_id = self.after(_PROGRESS_TICK_MS,
                                                 self._animate_progress)
        except Exception as e:  # noqa: BLE001
            print(f"[splash] 进度动画调度失败：{e}")

    def _finish_progress(self):
        """启动真的完成了：从当前位置平滑补满，而不是"啪"地跳到 100%。"""
        if self._closed or self._finishing:
            return
        self._finishing = True
        start = self._progress_value
        t0 = time.time()

        def _step():
            if self._closed:
                return
            k = ease_out_cubic((time.time() - t0) * 1000.0 / _FINISH_MS)
            self._set_progress(start + (1.0 - start) * k)
            if k >= 1.0:
                self._finish_after_id = None
                self._try_close()
                return
            try:
                self._finish_after_id = self.after(_FINISH_TICK_MS, _step)
            except Exception as e:  # noqa: BLE001
                print(f"[splash] 收尾动画调度失败：{e}")
                self._try_close()

        _step()

    # ------------------------------------------------------------------
    def _set_alpha(self, value: float):
        self._alpha = max(0.0, min(1.0, value))
        try:
            self.attributes("-alpha", self._alpha)
        except Exception:  # noqa: BLE001, S110
            pass  # 平台不支持 -alpha（如没有合成器的 X11）→ 退化成直接显示

    def _fade_in(self):
        """布局算完之后才露面：alpha 0 → 1，缓出。"""
        try:
            self.deiconify()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] deiconify 失败：{e}")
        self._set_alpha(0.0)
        t0 = time.time()

        def _step():
            if self._closed:
                return
            k = ease_out_cubic((time.time() - t0) * 1000.0 / _FADE_MS)
            self._set_alpha(k)
            if k >= 1.0:
                self._fade_after_id = None
                return
            try:
                self._fade_after_id = self.after(_FADE_TICK_MS, _step)
            except Exception as e:  # noqa: BLE001
                print(f"[splash] 淡入调度失败：{e}")
                self._set_alpha(1.0)

        _step()

    # ------------------------------------------------------------------
    def _schedule_boot(self, tasks: list):
        self._boot_started = time.time()
        self._force_after_id = self.after(self._max_ms, self._try_close)
        self._animate_progress()

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
        for aid in (self._force_after_id, self._close_after_id,
                    self._progress_after_id, self._fade_after_id,
                    self._finish_after_id):
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception as e:  # noqa: BLE001
                    print(f"[splash] 关闭前 after_cancel 失败：{e}")
        self._force_after_id = None
        self._close_after_id = None
        self._progress_after_id = None
        self._fade_after_id = None
        self._finish_after_id = None

        try:
            self.destroy()
        except Exception as e:  # noqa: BLE001
            print(f"[splash] destroy 失败：{e}")

        if self._on_done is not None:
            try:
                self._on_done()
            except Exception as e:  # noqa: BLE001
                print(f"[splash] on_done 回调失败：{e}")
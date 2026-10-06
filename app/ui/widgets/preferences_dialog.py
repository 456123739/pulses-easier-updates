"""
preferences_dialog.py — 首选项弹窗
------------------------------------------------
- 左侧分类：下载 / 比对 / 界面 / 关于
- 右侧表单：每项带悬浮说明
- 底部：恢复默认 / 取消 / 保存
"""

from collections.abc import Callable

import customtkinter as ctk

from ...core import database as db
from ...core import downloader as dl_mod
from ...theme import Color, Font, Size

_CATEGORIES = [
    ("download", "下载 · RideX", db.DEFAULT_DOWNLOAD_OPTIONS,
     db.DOWNLOAD_OPTION_META),
    ("compare", "比对", db.DEFAULT_COMPARE_OPTIONS,
     db.COMPARE_OPTION_META),
    ("ui", "界面", db.DEFAULT_UI_OPTIONS, db.UI_OPTION_META),
    ("about", "关于", None, None),
]

_INT_KEYS = {
    "multi_slots", "single_slots",
    "part_threads", "max_connections",
    "connect_timeout", "read_idle_timeout", "per_url_timeout",
    "stall_timeout", "min_speed_bps",
    "retry_same_url", "retry_backoff_ms",
    "single_url_timeout", "single_stall_timeout",
    "single_min_speed_bps",
    "hash_threads", "log_max_lines", "progress_throttle_ms",
    "apply_chunk_kb", "apply_batch_mb", "boot_min_ms", "boot_max_ms",
    "apply_space_check", "apply_deep_verify", "apply_clean_cache",
    # 下载引擎增量项（1/0 开关 + 整数）
    "part_retry", "http_status_check", "part_meta_enabled",
    "cleanup_residual_parts",
    # 按磁盘类型限制并发
    "disk_aware_slots", "hdd_max_files", "hdd_part_threads",
    "ssd_max_files", "ssd_part_threads",
    "network_max_files", "network_part_threads",
    "removable_max_files", "removable_part_threads",
    "disk_probe_fallback",
    # 分片策略：按大小分级
    "multi_part_min_bytes", "target_part_size", "max_part_count",
    "large_file_multi_first_bytes",
}
_FLOAT_KEYS = {"speed_window", "read_poll_interval", "disk_probe_timeout",
               "apply_chunk_pause_ms", "apply_batch_pause_ms"}


class _FieldRow:
    def __init__(self, parent, key: str, label: str,
                 desc: str, example: str, initial):
        self.key = key
        self.parent = parent

        row = ctk.CTkFrame(parent, fg_color=Color.TRANSPARENT)
        row.pack(fill="x", padx=0, pady=(0, 10))
        row.grid_columnconfigure(1, weight=1)

        self.label_widget = ctk.CTkLabel(
            row, text=label, font=Font.SMALL,
            text_color=Color.TEXT_PRIMARY,
            fg_color=Color.TRANSPARENT, anchor="w", width=200)
        self.label_widget.grid(row=0, column=0, sticky="w",
                               padx=(0, 12))

        self.var = ctk.StringVar(value=str(initial))
        self.entry = ctk.CTkEntry(
            row, textvariable=self.var, font=Font.BODY,
            fg_color=getattr(Color, "INPUT_BG", Color.LOG_BG),
            border_width=0, text_color=Color.TEXT_PRIMARY,
            height=32,
            corner_radius=getattr(Size, "RADIUS_INPUT", 6))
        self.entry.grid(row=0, column=1, sticky="ew")

        self._tip: ctk.CTkToplevel | None = None
        for w in (self.label_widget, self.entry):
            w.bind("<Enter>", lambda e, d=desc, x=example:
                   self._show_tip(d, x), add=True)  # type: ignore[arg-type]
            w.bind("<Leave>", lambda e: self._hide_tip(), add=True)  # type: ignore[arg-type]

    def _show_tip(self, desc: str, example: str):
        if self._tip is not None:
            return
        try:
            x = self.entry.winfo_rootx() + 10
            y = self.entry.winfo_rooty() + self.entry.winfo_height() + 6
            self._tip = ctk.CTkToplevel(self.parent)
            self._tip.wm_overrideredirect(True)
            self._tip.configure(fg_color=Color.LOG_BG)
            self._tip.geometry(f"+{x}+{y}")
            text = f"{desc}\n\n例如：{example}" if example else desc
            ctk.CTkLabel(
                self._tip, text=text, font=Font.SMALL,
                text_color=Color.TEXT_PRIMARY, fg_color=Color.LOG_BG,
                justify="left", wraplength=380, anchor="w")\
                .pack(padx=10, pady=8)
        except Exception:  # noqa: BLE001
            self._tip = None

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
            self._tip = None

    def get_value(self):
        raw = self.var.get().strip()
        if self.key in _INT_KEYS:
            try:
                return int(raw)
            except ValueError:
                return None
        if self.key in _FLOAT_KEYS:
            try:
                return float(raw)
            except ValueError:
                return None
        return raw


class PreferencesDialog(ctk.CTkToplevel):
    def __init__(self, parent,
                 on_saved: Callable[[], None] | None = None):
        super().__init__(parent)

        self.title("首选项")
        self.geometry("860x620")
        self.configure(fg_color=Color.WINDOW_BG)
        self.transient(parent)
        self.grab_set()
        self.minsize(760, 520)

        try:
            self.update_idletasks()
            px = parent.winfo_rootx() + (parent.winfo_width() - 860) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 620) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:  # noqa: BLE001, S110
            pass

        self._on_saved = on_saved
        self._current_cat = "download"
        self._rows: dict[str, _FieldRow] = {}
        self._cat_buttons: dict[str, ctk.CTkButton] = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_sidebar()
        self._build_body()
        self._build_footer()

        self._switch_category("download")

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                              corner_radius=0, height=52)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="首选项",
                     font=(Font.FAMILY, 15, "bold"),
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, sticky="w", padx=20)

        ctk.CTkButton(header, text="✕", width=32, height=32,
                      font=Font.BODY, corner_radius=6,
                      fg_color=Color.TRANSPARENT,
                      hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self.destroy)\
            .grid(row=0, column=1, sticky="e", padx=12)

    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, fg_color=Color.SIDEBAR_BG,
                           corner_radius=0, width=160)
        bar.grid(row=1, column=0, sticky="nsw")
        bar.grid_propagate(False)

        for i, (key, label, _defaults, _meta) in enumerate(_CATEGORIES):
            btn = ctk.CTkButton(
                bar, text=label, anchor="w",
                font=Font.BODY, height=36,
                fg_color=Color.TRANSPARENT,
                hover_color=Color.BORDER,
                text_color=Color.TEXT_PRIMARY,
                corner_radius=6,
                command=lambda k=key: self._switch_category(k))
            btn.pack(fill="x", padx=10, pady=(10 if i == 0 else 2, 0))
            self._cat_buttons[key] = btn

    def _build_body(self):
        self.body = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                                 corner_radius=0)
        self.body.grid(row=1, column=1, sticky="nsew",
                       padx=12, pady=12)
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        self.form_scroll = ctk.CTkScrollableFrame(
            self.body, fg_color=Color.CARD_BG, corner_radius=0,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.form_scroll.grid(row=0, column=0, sticky="nsew")
        self.form_scroll.grid_columnconfigure(0, weight=1)

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                              corner_radius=0, height=60)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.grid_propagate(False)

        ctk.CTkButton(footer, text="恢复默认", width=100, height=36,
                      font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_PRIMARY,
                      command=self._on_reset)\
            .pack(side="left", padx=20, pady=12)

        ctk.CTkButton(footer, text="保存", width=100, height=36,
                      font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
                      text_color=Color.TEXT_ON_ACCENT,
                      command=self._on_save)\
            .pack(side="right", padx=12, pady=12)

        ctk.CTkButton(footer, text="取消", width=100, height=36,
                      font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_PRIMARY,
                      command=self.destroy)\
            .pack(side="right", padx=0, pady=12)

    def _switch_category(self, key: str):
        self._current_cat = key
        for k, btn in self._cat_buttons.items():
            if k == key:
                btn.configure(fg_color=Color.ACCENT,
                              text_color=Color.TEXT_ON_ACCENT,
                              hover_color=Color.ACCENT_HOVER)
            else:
                btn.configure(fg_color=Color.TRANSPARENT,
                              text_color=Color.TEXT_PRIMARY,
                              hover_color=Color.BORDER)

        for w in self.form_scroll.winfo_children():
            w.destroy()
        self._rows.clear()

        if key == "about":
            self._build_about()
            return

        cat = next((c for c in _CATEGORIES if c[0] == key), None)
        if cat is None:
            return
        _, _label, defaults, meta = cat
        if defaults is None or meta is None:
            return

        current = self._load_current(key)
        hidden = db._HIDDEN_DOWNLOAD_KEYS if key == "download" else set()
        for opt_key, opt_label, desc, example in meta:
            if opt_key in hidden:
                continue
            initial = current.get(opt_key, defaults.get(opt_key, ""))
            row = _FieldRow(self.form_scroll, opt_key, opt_label,
                            desc, example, initial)
            self._rows[opt_key] = row

    def _build_about(self):
        frame = ctk.CTkFrame(self.form_scroll, fg_color=Color.CARD_BG)
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        try:
            from ...core import __version__ as ver
        except Exception:  # noqa: BLE001
            ver = "开发版"

        try:
            engine_line = dl_mod.engine_banner()
        except Exception:  # noqa: BLE001
            engine_line = "RideX（锐驰引擎）"

        lines = [
            ("Pulses Easier", 18, "bold"),
            ("版本 " + str(ver), 12, "normal"),
            ("下载引擎：" + engine_line, 11, "normal"),
            ("", 6, "normal"),
            ("Produced by Pulses0 Studio.", 12, "normal"),
            ("Made by NimShade & DS.", 12, "normal"),
            ("", 6, "normal"),
            ("—— 致谢 ——", 12, "bold"),
            ("PCL2 下载引擎设计思路", 11, "normal"),
            ("CustomTkinter / multimark / pywebview", 11, "normal"),
        ]
        for text, size, weight in lines:
            if not text:
                ctk.CTkFrame(frame, fg_color=Color.TRANSPARENT,
                             height=size).pack()
                continue
            ctk.CTkLabel(frame, text=text,
                         font=(Font.FAMILY, size, weight),
                         text_color=Color.TEXT_PRIMARY
                         if weight == "bold" else Color.TEXT_SECONDARY,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .pack(anchor="w", pady=2)

    def _load_current(self, key: str) -> dict:
        try:
            if key == "download":
                return db.get_download_options()
            if key == "compare":
                return db.get_compare_options()
            if key == "ui":
                return db.get_ui_options()
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _on_save(self):
        values: dict = {}
        for k, row in self._rows.items():
            v = row.get_value()
            if v is None:
                self._toast(f"配置项「{k}」格式不正确")
                return
            values[k] = v

        try:
            if self._current_cat == "download":
                db.set_download_options(values)
                dl_mod.invalidate_options()
            elif self._current_cat == "compare":
                db.set_compare_options(values)
            elif self._current_cat == "ui":
                db.set_ui_options(values)
        except Exception as e:  # noqa: BLE001
            self._toast(f"保存失败：{e}")
            return

        if self._on_saved is not None:
            try:
                self._on_saved()
            except Exception:  # noqa: BLE001, S110
                pass

        self._toast("已保存", level="success")
        self.after(400, self.destroy)

    def _on_reset(self):
        cat = next((c for c in _CATEGORIES
                    if c[0] == self._current_cat), None)
        if cat is None or cat[2] is None:
            return
        defaults = cat[2]
        for k, row in self._rows.items():
            if k in defaults:
                row.var.set(str(defaults[k]))

    def _toast(self, message: str, level: str = "warn"):
        try:
            from .dialog import alert
            alert(self, "首选项", message, level=level)
        except Exception:  # noqa: BLE001, S110
            pass


def open_preferences(parent,
                     on_saved: Callable[[], None] | None = None):
    return PreferencesDialog(parent, on_saved=on_saved)
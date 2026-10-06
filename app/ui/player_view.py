"""
player_view.py — 玩家界面
------------------------------------------------
- 六态按钮：禁用 / 开始更新 / 确认更新 / 重新扫描 / 继续更新 / 处理中
- 策略改动 → 按钮变「重新扫描」，重算任务并更新变更列表标记
- 下载中断可恢复：resume.json
- 关闭守卫：下载/应用阶段点 X 弹自定义确认弹窗
- 双层叠加进度条（整体 + 当前文件）
- 下载文字：「正在下载：<文件> · 速度 · 12/34」
- 更新日志预渲染：导入后后台渲染，按钮状态跟随
- 槽位面板：点击进度条展开，显示各并行文件的进度和速度
"""

import shutil
import threading
import zipfile
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

import customtkinter as ctk

from ..config import Strategy, default_strategy_for_dir
from ..core.apply_rules import resolve_strategy
from ..core import cache as cache_mod
from ..core import checkpoint as cp_mod
from ..core import database as db
from ..core import eapack as eapack_mod
from ..core import resume as resume_mod
from ..core.apply_rules import is_checked as _rule_is_checked
from ..core.differ import DiffResult, diff_packs_parallel
from ..core.downloader import DownloadTask, download_files
from ..core import pending as pending_logic
from ..core.mrpack import (
    index_top_folders,
    is_reserved_root_name,
    locate_content_root,
    merge_files,
    parse_mrpack,
    top_level_folders,
)
from ..core.pack_detector import PackKind, detect_pack_kind
from ..core.pack_info import ModpackInfo, read_modpack_info
from ..core.state import AppState
from ..core import recover as recover_mod
from ..core import transfer
from ..core.updater import (
    PlanSource,
    SourceLayer,
    UpdatePlan,
    build_plan,
    execute_plan,
    fill_expectations as updater_fill,
    verify_after_update,
)
from ..theme import Color, Font, Size
from .widgets.change_list import ChangeList
from .widgets.dialog import confirm
from .widgets.drop_zone import DropZone
from .widgets.log_panel import LogPanel
from .widgets.pending_banner import PendingBanner
from .widgets.pending_window import PendingWindow
from .widgets.progress_bar import SmoothProgressBar
from .widgets.slot_panel import SlotPanel
from .widgets.strategy_table import StrategyTable

_BTN_DISABLED = "disabled"
_BTN_READY = "ready"
_BTN_CONFIRM = "confirm"
_BTN_BUSY = "busy"
_BTN_RESUME = "resume"
_BTN_RESCAN = "rescan"

_RIGHT_MIN_W = 360
_HINT_H = 22
_SPEED_SAMPLES = 6

_PHASE_IDLE = "idle"
_PHASE_SCAN = "scan"
_PHASE_DOWNLOAD = "download"
_PHASE_APPLY = "apply"
_PHASE_MANUAL = "manual"      # 等待用户手动补入失败文件


class PlayerView(ctk.CTkFrame):
    def __init__(self, master, sidebar, app_state: AppState, **kwargs):
        super().__init__(master, fg_color=Color.WORKSPACE_BG,
                         corner_radius=0, **kwargs)
        self.sidebar = sidebar
        self.app_state = app_state

        self.pack_info: ModpackInfo | None = None
        self.update_zip: Path | None = None
        self.pack_kind: PackKind = PackKind.UNKNOWN
        self._temp_dir: TemporaryDirectory | None = None
        self._new_root: Path | None = None
        self._overrides_root: Path | None = None
        # 更新包索引里的期望哈希（rel → {algo: value}）：跨卷搬运时
        # 顺带做内容级校验，不额外读一遍盘
        self._index_hashes: dict[str, dict[str, str]] = {}
        # True = 包内没有 overrides/，内容根退化为解压根目录（老格式）；
        # 这种模式下必须排除包自身的元数据文件（否则会被当成更新内容）
        self._overrides_fallback: bool = False
        self._cache_root: Path | None = None
        self.diff: DiffResult | None = None
        self._merged: list[dict] = []
        self.plan: UpdatePlan | None = None
        self._changelog_md: str = ""
        self._changelog_html: str = ""
        self._changelog_rendering: bool = False
        self._changelog_render_token: int = 0
        self._download_tasks: list[DownloadTask] = []
        self._completed_files: list[str] = []
        self._btn_state: str = _BTN_DISABLED
        self._phase: str = _PHASE_IDLE
        self._phase_started_at: float = 0.0
        self._abort_flag: bool = False
        self._download_thread: threading.Thread | None = None
        self._last_results: list = []
        self._resume_after_scan: bool = False
        # ---- 「更新受阻 / 补齐缺口」状态 ----
        self.pending_banner: PendingBanner | None = None
        self.pending_window: PendingWindow | None = None
        # wait  = 本次下载有文件失败，等玩家补入
        # complete = 上次"放弃"留下的缺口，本次定位整合包后补齐
        self._pending_mode: str = "wait"
        self._pending_errors: dict[str, str] = {}
        self._pending_ready: bool = False     # 已补齐，但还有别的文件在下
        self._give_up: bool = False
        self._give_up_missing: list[dict] = []
        self._pending_auto_opened: bool = False
        self._record_marker: dict | None = None
        self._record_cache_root: Path | None = None
        self._record_rels: list[str] = []
        # 续传时恢复玩家上次自定义的策略
        self._resume_strategies: dict = {}
        self._resume_checked: dict = {}
        # 扫描后的"基线策略"（默认值 + 包内推荐值），用来判断玩家是否改过
        self._baseline_strategies: dict = {}

        self._strategy_snapshot: dict = {"checked": {}, "strategies": {}}

        self._speed_rel: str = ""
        self._speed_samples: deque[tuple[float, int]] = deque(
            maxlen=_SPEED_SAMPLES)
        # 字节级进度节流（首选项 ui.progress_throttle_ms）：
        # 下载回调是"每个读取块一次"，不节流会把 Tk 事件队列打满
        self._byte_throttle_s: float = 0.2
        self._byte_last_at: dict[str, float] = {}
        self._current_rel: str = ""
        self._done_count: int = 0
        self._total_count: int = 0

        # 槽位面板状态
        self._slot_expanded: bool = False
        self._active_files: dict[str, dict] = {}
        self._file_speed_samples: dict[
            str, deque[tuple[float, int]]] = {}
        self._slot_refresh_after_id: str | None = None

        self._build()
        self.app_state.on_modpack_changed(self._on_modpack_changed)
        self.app_state.on_lock_changed(self._on_lock_changed)
        self._refresh_ui_state()

    # ------------------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=Size.PLAYER_L_RATIO)
        self.grid_columnconfigure(1, weight=Size.PLAYER_R_RATIO,
                                  minsize=_RIGHT_MIN_W)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_right()

    def _build_left(self):
        self.left = ctk.CTkFrame(self, fg_color=Color.WORKSPACE_BG,
                                 corner_radius=0)
        self.left.grid(row=0, column=0, sticky="nsew",
                       padx=(Size.PAD_WORKSPACE, Size.GAP // 2),
                       pady=Size.PAD_WORKSPACE)
        self.left.grid_columnconfigure(0, weight=1)
        self.left.grid_rowconfigure(1, weight=1)

        drop_wrap = ctk.CTkFrame(self.left, fg_color=Color.TRANSPARENT)
        drop_wrap.grid(row=0, column=0, sticky="ew")
        drop_wrap.grid_columnconfigure(0, weight=1)
        self._drop_wrap = drop_wrap

        self.drop_zip = DropZone(
            drop_wrap,
            title="STEP 2 · 拖入更新包",
            subtitle="支持 .zip / .eapack",
            height=Size.DROP_H_WORK,
            mode="zip",
            on_drop=self._on_update_pack_dropped,
            highlight=True)
        self.drop_zip.grid(row=0, column=0, sticky="ew")

        self.clear_pack_btn = ctk.CTkButton(
            drop_wrap, text="清空", width=80, height=28,
            font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._on_clear_pack_click)
        self.clear_pack_btn.grid(row=1, column=0, sticky="e", pady=(6, 0))
        drop_wrap.grid_remove()

        self.strategy_table = StrategyTable(
            self.left, title="更新策略",
            protected_folders=["mods"],
            on_change=self._on_strategy_changed)
        self.strategy_table.grid(row=1, column=0, sticky="nsew",
                                 pady=(Size.GAP, 0))
        self.strategy_table.grid_remove()

        self.main_btn = ctk.CTkButton(
            self.left, text="开始更新", height=Size.MAIN_BTN_H,
            font=Font.BTN_LARGE, corner_radius=Size.RADIUS_DROP,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            state="disabled",
            command=self._on_main_click)
        self.main_btn.grid(row=2, column=0, sticky="ew",
                           pady=(Size.GAP, 0))
        self.main_btn.grid_remove()

    def _build_right(self):
        self.right = ctk.CTkFrame(self, fg_color=Color.WORKSPACE_BG,
                                  corner_radius=0)
        self.right.grid(row=0, column=1, sticky="nsew",
                        padx=(Size.GAP // 2, Size.PAD_WORKSPACE),
                        pady=Size.PAD_WORKSPACE)
        self.right.grid_columnconfigure(0, weight=1, minsize=_RIGHT_MIN_W)
        self.right.grid_rowconfigure(0, weight=1)
        self.right.grid_propagate(False)

        self.change_list = ChangeList(self.right)
        self.change_list.grid(row=0, column=0, sticky="nsew")
        self.change_list.grid_remove()


        self.placeholder = ctk.CTkFrame(
            self.right, fg_color=Color.CARD_BG,
            corner_radius=Size.RADIUS_CARD)
        self.placeholder.grid(row=0, column=0, sticky="nsew")
        self._freed_cache_bytes = 0
        self._placeholder_text = "请先在侧边栏定位整合包，再拖入更新包"
        self.placeholder_label = ctk.CTkLabel(
            self.placeholder,
            text=self._placeholder_text,
            font=Font.BODY, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT)
        self.placeholder_label.place(relx=0.5, rely=0.5, anchor="center")

        self.progress_wrap = ctk.CTkFrame(
            self.right, fg_color=Color.TRANSPARENT,
            height=SmoothProgressBar._TOTAL_H)
        self.progress_wrap.grid(row=1, column=0, sticky="ew",
                                pady=(Size.GAP, 0))
        self.progress_wrap.grid_propagate(False)
        self.progress_wrap.pack_propagate(False)

        self.progress = SmoothProgressBar(self.progress_wrap)
        self.progress.pack(fill="x", expand=True)
        # 点击总进度条展开槽位面板
        try:
            self.progress.bind("<Button-1>",
                               lambda e: self._toggle_slot_panel())
        except Exception:  # noqa: BLE001, S110
            pass
        self.progress_wrap.grid_remove()

        # 槽位展开面板（默认隐藏）
        try:
            _slots = int(db.get_download_options().get("multi_slots", 12))
        except Exception:  # noqa: BLE001
            _slots = 12
        self.slot_panel: SlotPanel = SlotPanel(
            self.right, slots=_slots, on_toggle=self._on_slot_toggle)
        self.slot_panel.grid(row=5, column=0, sticky="ew",
                             pady=(Size.GAP, 0))
        self.slot_panel.grid_remove()

        self.hint_wrap = ctk.CTkFrame(
            self.right, fg_color=Color.TRANSPARENT, height=_HINT_H)
        self.hint_wrap.grid(row=2, column=0, sticky="ew",
                            pady=(4, 0))
        self.hint_wrap.grid_propagate(False)
        self.hint_wrap.pack_propagate(False)

        self.progress_label = ctk.CTkLabel(
            self.hint_wrap, text="",
            font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.progress_label.pack(fill="both", expand=True)
        self.hint_wrap.grid_remove()

        self.log = LogPanel(self.right, title="日志",
                            on_clear=self._on_log_clear)
        self.log.grid(row=3, column=0, sticky="ew", pady=(Size.GAP, 0))

        self.changelog_btn = ctk.CTkButton(
            self.right, text="查看更新日志", height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.CARD_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            state="disabled",
            command=self._show_changelog)
        self.changelog_btn.grid(row=4, column=0, sticky="w",
                                pady=(Size.GAP, 0))

    # ------------------------------------------------------------------
    def _refresh_ui_state(self):
        modpack_ready = self.pack_info is not None
        pack_ready = self.update_zip is not None

        if modpack_ready:
            self._drop_wrap.grid()
        else:
            self._drop_wrap.grid_remove()

        if pack_ready and self.diff is not None:
            self.strategy_table.grid()
        else:
            self.strategy_table.grid_remove()

        if pack_ready:
            self.main_btn.grid()
            self._set_button_state(self._btn_state)
        else:
            self.main_btn.grid_remove()

        has_pending = self.pending_banner is not None
        if self.diff is not None and not has_pending:
            self.change_list.grid()
            self.placeholder.grid_remove()

        if not has_pending and self.diff is None:
            self.change_list.grid_remove()
            self.placeholder.grid()

        self._refresh_changelog_button()

    def _set_button_state(self, state: str):
        self._btn_state = state
        try:
            if state == _BTN_DISABLED:
                self.main_btn.configure(
                    text="开始更新", state="disabled",
                    fg_color=Color.ACCENT,
                    hover_color=Color.ACCENT_HOVER,
                    text_color=Color.TEXT_ON_ACCENT)
            elif state == _BTN_READY:
                self.main_btn.configure(
                    text="开始更新", state="normal",
                    fg_color=Color.ACCENT,
                    hover_color=Color.ACCENT_HOVER,
                    text_color=Color.TEXT_ON_ACCENT)
            elif state == _BTN_CONFIRM:
                self.main_btn.configure(
                    text="确认更新", state="normal",
                    fg_color=Color.MODIFIED,
                    hover_color="#C8C88A",
                    text_color=Color.ACCENT)
            elif state == _BTN_RESCAN:
                self.main_btn.configure(
                    text="重新扫描", state="normal",
                    fg_color=Color.LOG_BG,
                    hover_color=Color.BORDER,
                    text_color=Color.TEXT_PRIMARY)
            elif state == _BTN_RESUME:
                self.main_btn.configure(
                    text="继续更新", state="normal",
                    fg_color=Color.MODIFIED,
                    hover_color="#C8C88A",
                    text_color=Color.ACCENT)
            elif state == _BTN_BUSY:
                self.main_btn.configure(
                    text="处理中…", state="disabled",
                    fg_color=Color.BORDER,
                    hover_color=Color.BORDER,
                    text_color=Color.TEXT_SECONDARY)
        except Exception:  # noqa: BLE001, S110
            pass

    def _set_hint(self, text: str, color: str | None = None):
        try:
            self.progress_label.configure(
                text=text,
                text_color=color or Color.TEXT_SECONDARY)
            if text:
                self.hint_wrap.grid()
            else:
                self.hint_wrap.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # 槽位面板
    # ------------------------------------------------------------------
    def _toggle_slot_panel(self):
        self._on_slot_toggle(not self._slot_expanded)

    def _on_slot_toggle(self, expand: bool):
        self._slot_expanded = bool(expand)
        try:
            if self._slot_expanded:
                self.slot_panel.grid()
            else:
                self.slot_panel.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass
        self._refresh_slots()

    def _refresh_slots(self):
        if not self._slot_expanded:
            return
        try:
            active_rows: list[dict] = []
            for rel, info in list(self._active_files.items()):
                samples = self._file_speed_samples.get(rel)
                bps = 0.0
                if samples and len(samples) >= 2:
                    t0, b0 = samples[0]
                    t1, b1 = samples[-1]
                    dt = t1 - t0
                    if dt > 0:
                        delta = b1 - b0
                        bps = max(0.0, delta / dt)
                total = info.get("total", 0)
                recv = info.get("received", 0)
                progress = (recv / total) if total > 0 else 0.0
                active_rows.append({
                    "rel": rel,
                    "progress": progress,
                    "speed_bps": bps,
                })
            active_rows.sort(key=lambda x: x["speed_bps"], reverse=True)

            n_raw = self.slot_panel.slot_count()
            n: int = int(n_raw) if n_raw else 0
            rows: list[dict | None] = list(active_rows)
            while len(rows) < n:
                rows.append(None)
            self.slot_panel.update(rows[:n])
        except Exception:  # noqa: BLE001, S110
            pass

    def _schedule_slot_refresh(self):
        if self._slot_refresh_after_id is not None:
            return
        try:
            self._slot_refresh_after_id = self.after(
                150, self._do_slot_refresh)
        except Exception:  # noqa: BLE001, S110
            self._slot_refresh_after_id = None

    def _do_slot_refresh(self):
        self._slot_refresh_after_id = None
        self._refresh_slots()

    # ------------------------------------------------------------------
    def _on_modpack_changed(self, path: Path | None):
        if self._phase != _PHASE_IDLE:
            # 更新流程进行中：不许换包/清空（否则会用错的整合包执行更新）。
            # 侧边栏此时也被 app_state.lock() 挡住，这里是兜底。
            self.log.log("warn", "更新流程进行中，暂时不能更换整合包")
            return
        if path is None:
            self.pack_info = None
            self._reset_pack_state()
            self._refresh_ui_state()
            return

        try:
            new_info = read_modpack_info(path)
        except Exception:  # noqa: BLE001
            new_info = None

        old_path = self.pack_info.path if self.pack_info is not None else None
        new_path = new_info.path if new_info is not None else None
        if old_path is not None and new_path is not None \
                and Path(old_path) != Path(new_path):
            # 换了整合包：针对旧包算出来的 diff / 下载任务必须作废，
            # 否则会拿旧比对结果去写新包（误删新包里的文件）
            self.log.log("warn", "已更换整合包，需要重新进行比对")
            self._reset_pack_state()
            self._set_hint("已更换整合包，请拖入更新包后重新开始")

        self.pack_info = new_info

        if self.pack_info is not None:
            self.log.clear()
            self.log.log(
                "info",
                f"已定位整合包：{self.pack_info.name}"
                f"（{self.pack_info.mod_count} 个模组）")
            self.log.log("info", "请拖入更新包（.zip / .eapack）")
            self._set_hint("拖入更新包后即可开始")
            # 上次"放弃"留下的缺口：等界面稳定后询问是否现在补齐
            self.after(250, self._check_pending_record)
        self._refresh_ui_state()

    def _on_lock_changed(self, locked: bool):
        self.drop_zip.set_text(
            title="更新进行中…" if locked else "STEP 2 · 拖入更新包",
            subtitle="请稍候" if locked else "支持 .zip / .eapack")

    # ------------------------------------------------------------------
    # 策略变化
    # ------------------------------------------------------------------
    def _on_strategy_changed(self, source: str = "external"):
        try:
            self.change_list.update_marks(
                checked=self.strategy_table.get_checked(),
                strategies=self.strategy_table.get_strategies())
        except Exception:  # noqa: BLE001, S110
            pass

        if source == "external":
            return
        if self.diff is None:
            return
        if self._phase != _PHASE_IDLE:
            return

        dirty = self._is_strategy_dirty()
        if dirty and self._btn_state in (_BTN_CONFIRM, _BTN_RESCAN):
            self._set_button_state(_BTN_RESCAN)
        elif not dirty and self._btn_state == _BTN_RESCAN:
            self._set_button_state(_BTN_CONFIRM)

    def _restore_resume_strategies(self) -> bool:
        """
        续传时把玩家上次自定义的勾选/策略恢复回策略表。

        规则（按用户确认）：**玩家自定义过就沿用玩家的**；
        没自定义过（等于默认值 + 包内推荐值）就不动，直接用当前默认。
        """
        strat = dict(self._resume_strategies or {})
        checked = dict(self._resume_checked or {})
        if not strat and not checked:
            return False
        try:
            self._resume_strategies, self._resume_checked = {}, {}
            if checked:
                blacklist = [name for name, ok in checked.items() if not ok]
                self.strategy_table.set_blacklist(blacklist)
            if strat:
                self.strategy_table.set_strategies(strat)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _is_strategy_customized(self) -> bool:
        """当前策略是否偏离了"默认 + 包内推荐"的基线。"""
        if not self._baseline_strategies:
            return False
        cur = self.strategy_table.get_strategies()
        if cur != self._baseline_strategies:
            return True
        checked = self.strategy_table.get_checked()
        return any(not v for v in checked.values())

    def _is_strategy_dirty(self) -> bool:
        cur = {
            "checked": self.strategy_table.get_checked(),
            "strategies": self.strategy_table.get_strategies(),
        }
        return cur != self._strategy_snapshot

    def _take_strategy_snapshot(self):
        self._strategy_snapshot = {
            "checked": self.strategy_table.get_checked(),
            "strategies": self.strategy_table.get_strategies(),
        }

    # ------------------------------------------------------------------
    def _on_update_pack_dropped(self, paths: list[Path]):
        if self._phase != _PHASE_IDLE:
            self.log.log("warn", "更新流程进行中，暂时不能更换更新包")
            return
        if self.app_state.is_locked:
            return

        candidates = [
            p for p in paths
            if p.suffix.lower() in (".zip", ".mrpack", ".eapack")
        ]
        if not candidates:
            self.log.log("error", "请拖入 .zip 或 .eapack 文件")
            return

        zip_path = candidates[0]
        kind, err = detect_pack_kind(zip_path)
        if kind == PackKind.UNKNOWN:
            self.log.log("error", f"无法识别：{err}")
            return

        # 换包：旧包算出来的 diff/任务/解压目录全部作废
        self._cleanup_temp_dir()
        self.diff = None
        self.plan = None
        self._merged = []
        self._download_tasks = []
        self._completed_files = []
        self._overrides_root = None
        self._new_root = None
        self._overrides_fallback = False
        self.strategy_table.set_folders([])
        self.change_list.load(None)

        self.update_zip = zip_path
        self.pack_kind = kind
        self._cache_root = self._resolve_cache_root(zip_path)

        # 完整性校验（导出时勾选"防篡改"才有签名）
        try:
            ok, reason = eapack_mod.verify_signature(zip_path)
            if not ok:
                self.log.log("warn", f"⚠ 完整性校验未通过：{reason}")
                self.log.log("warn", "更新包可能被改动过；"
                                     "每个文件仍会按 index 哈希校验")
            elif reason == "":
                self.log.log("info", "更新包完整性校验通过")
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            self.drop_zip.set_highlight(False)
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            size = zip_path.stat().st_size
        except OSError:
            size = 0
        from ..utils.files import human_size
        kind_label = "更新包" if kind == PackKind.UPDATE else "导出包"
        self.drop_zip.set_text(
            title=f"{kind_label}：{zip_path.name}",
            subtitle=human_size(size))

        self.log.clear()
        if self.pack_info is not None:
            self.log.log(
                "info",
                f"整合包：{self.pack_info.name}"
                f"（{self.pack_info.mod_count} 个模组）")
        self.log.log("info", f"已导入{kind_label}：{zip_path.name}")
        self._set_hint("点击「开始更新」按钮以开始文件比对")

        # 清空旧缓存
        self._changelog_md = ""
        self._changelog_html = ""
        self._changelog_rendering = False
        self._changelog_render_token += 1

        try:
            md = eapack_mod.read_changelog(zip_path)
            if md:
                self._changelog_md = md
                self.log.log("info", "已读取更新日志，正在后台渲染…")
                self._start_changelog_render(self._changelog_render_token)
            else:
                self._changelog_md = ""
        except Exception:  # noqa: BLE001
            self._changelog_md = ""

        if self._btn_state != _BTN_RESUME:
            self._set_button_state(_BTN_READY)
        self._refresh_ui_state()
        self._refresh_changelog_button()

    # ------------------------------------------------------------------
    # 更新日志预渲染
    # ------------------------------------------------------------------
    def _start_changelog_render(self, token: int):
        self._changelog_rendering = True
        self._refresh_changelog_button()

        md = self._changelog_md

        def _worker():
            try:
                from ..core.markdown_renderer import render_markdown_sync
                ok, html = render_markdown_sync(md, options=None)
            except Exception as e:  # noqa: BLE001
                ok, html = False, f"渲染失败：{e}"
            self.after(0, lambda o=ok, h=html, t=token:
                       self._on_changelog_rendered(o, h, t))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_changelog_rendered(self, ok: bool, html: str, token: int):
        if token != self._changelog_render_token:
            return
        self._changelog_rendering = False
        if ok:
            self._changelog_html = html
            self.log.log("info", "更新日志已就绪")
        else:
            self._changelog_html = ""
            self.log.log("warn", f"更新日志渲染失败：{html}")
        self._refresh_changelog_button()

    def _refresh_changelog_button(self):
        try:
            if self._changelog_rendering:
                self.changelog_btn.configure(
                    text="加载中…", state="disabled",
                    text_color=Color.TEXT_MUTED,
                    fg_color=Color.LOG_BG,
                    hover_color=Color.LOG_BG)
                return
            if not self._changelog_md:
                self.changelog_btn.configure(
                    text="查看更新日志", state="disabled",
                    text_color=Color.TEXT_MUTED,
                    fg_color=Color.CARD_BG,
                    hover_color=Color.CARD_BG)
                return
            if self._changelog_html:
                self.changelog_btn.configure(
                    text="查看更新日志", state="normal",
                    text_color=Color.TEXT_PRIMARY,
                    fg_color=Color.CARD_BG,
                    hover_color=Color.BORDER)
            else:
                self.changelog_btn.configure(
                    text="查看更新日志（渲染失败）",
                    state="normal",
                    text_color=Color.WARNING,
                    fg_color=Color.CARD_BG,
                    hover_color=Color.BORDER)
        except Exception:  # noqa: BLE001, S110
            pass

    @staticmethod
    def _resolve_cache_root(zip_path: Path) -> Path | None:
        """
        下载缓存目录：<db>/cache/update_packs/<安全文件名>-<内容指纹>/

        指纹（大小 + 首尾 64KB 的 SHA-256）保证**同名不同内容**的更新包
        不会复用同一个目录 —— 否则上一个包遗留的文件会被当成新内容写进
        整合包（陈旧文件覆盖正确文件）。
        """
        db_root = db.get_db_path()
        if db_root is None:
            return None
        safe_name = "".join(
            c if c.isalnum() or c in "-_." else "_"
            for c in zip_path.stem)[:48] or "pack"
        try:
            fp = resume_mod.quick_fingerprint(zip_path)[:10]
        except Exception:  # noqa: BLE001
            fp = ""
        safe_name = f"{safe_name}-{fp}" if fp else safe_name
        cache = db_root / "cache" / "update_packs" / safe_name
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            return None
        return cache

    def _cleanup_temp_dir(self):
        tmp = self._temp_dir
        self._temp_dir = None
        if tmp is None:
            return
        try:
            tmp.cleanup()
        except Exception:  # noqa: BLE001, S110
            pass

    def _make_work_dir(self) -> TemporaryDirectory | None:
        """
        解压/合并用的工作目录：优先放在 <db>/cache/temp（"清理缓存"能覆盖
        到，"清理下载残留"启动任务也会清），数据库不可用时退回系统临时目录。
        """
        try:
            root = cache_mod.work_root()
        except Exception:  # noqa: BLE001
            root = None
        try:
            if root is not None:
                root.mkdir(parents=True, exist_ok=True)
                return TemporaryDirectory(prefix="pulses_play_", dir=str(root))
        except Exception:  # noqa: BLE001
            pass
        try:
            return TemporaryDirectory(prefix="pulses_play_")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def resume_from(self, data: dict, avail: dict | None = None) -> bool:
        try:
            zip_path = Path(
                (avail or {}).get("zip_path")
                or data.get("zip_path", ""))
            pack_root = Path(
                (avail or {}).get("pack_root")
                or data.get("pack_root", ""))
            if not zip_path.is_file():
                return False
            if not pack_root.is_dir():
                return False
        except Exception:  # noqa: BLE001
            return False

        self._btn_state = _BTN_RESUME

        try:
            self.app_state.set_modpack(pack_root)
        except Exception:  # noqa: BLE001, S110
            pass

        self._on_update_pack_dropped([zip_path])

        try:
            self._completed_files = list(data.get("completed", []))
        except Exception:  # noqa: BLE001
            self._completed_files = []

        # 玩家上次为这个更新包自定义过的策略：续传时要沿用
        self._resume_strategies = {}
        self._resume_checked = {}
        if data.get("strategies_customized"):
            try:
                strat = data.get("strategies") or {}
                if isinstance(strat, dict):
                    self._resume_strategies = {str(k): str(v)
                                               for k, v in strat.items()}
                chk = data.get("checked") or {}
                if isinstance(chk, dict):
                    self._resume_checked = {str(k): bool(v)
                                            for k, v in chk.items()}
            except Exception:  # noqa: BLE001
                self._resume_strategies, self._resume_checked = {}, {}

        self._resume_after_scan = False
        self._set_button_state(_BTN_RESUME)
        self._refresh_ui_state()

        self.log.log("info",
                     f"从上次中断处恢复，已下完 "
                     f"{len(self._completed_files)} 个文件")
        self._set_hint(
            "点击「继续更新」重新比对并继续下载剩余文件")
        return True

    # ------------------------------------------------------------------
    def _on_main_click(self):
        if self._btn_state == _BTN_READY:
            self._start_scan()
        elif self._btn_state == _BTN_RESUME:
            self._resume_after_scan = True
            self._start_scan()
        elif self._btn_state == _BTN_RESCAN:
            self._recompute_tasks()
        elif self._btn_state == _BTN_CONFIRM:
            self._start_download_and_apply()

    # ------------------------------------------------------------------
    def _recompute_tasks(self):
        if self.diff is None:
            return
        try:
            checked = self.strategy_table.get_checked()
            strategies = self.strategy_table.get_strategies()
            self._download_tasks = self._collect_download_tasks_from_diff(
                self.diff, self._merged, self._overrides_root,
                checked=checked, strategies=strategies)
            self.change_list.update_marks(checked=checked,
                                          strategies=strategies)
            self._take_strategy_snapshot()
            self._set_button_state(_BTN_CONFIRM)

            n = len(self._download_tasks)
            if n:
                self._set_hint(
                    f"策略已更新，需下载 {n} 个文件，"
                    f"点击「确认更新」开始")
            else:
                self._set_hint(
                    "策略已更新，无文件需要下载，点击「确认更新」应用")
            self.log.log("info", f"重新扫描完成：待下载 {n} 个文件")
        except Exception as e:  # noqa: BLE001
            self.log.log("error", f"重新扫描失败：{e}")

    # ------------------------------------------------------------------
    # 阶段 1：比对
    # ------------------------------------------------------------------
    def _start_scan(self):
        if self._phase != _PHASE_IDLE:
            # 早退时清掉"扫描完自动继续下载"的标记，否则下一次普通
            # 「开始更新」会莫名其妙跳过确认直接下载+应用
            self._resume_after_scan = False
            return
        if self.update_zip is None or self.pack_info is None:
            self._resume_after_scan = False
            return

        self._phase = _PHASE_SCAN
        self._set_button_state(_BTN_BUSY)
        self.progress_wrap.grid()
        self.progress.set_file_progress(None)
        self.progress.set(0.0, animate=False)
        self._set_hint("准备比对…")
        self._phase_started_at = time()

        def worker():
            try:
                self._scan_worker()
                self.after(0, self._on_scan_done)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.after(0, lambda m=msg:
                           self._on_error(f"比对失败：{m}"))

        threading.Thread(target=worker, daemon=True).start()

    def _recover_apply_leftovers(self):
        """就地自检（幂等、便宜）：回滚没做完的目录替换、删搬运临时文件。"""
        if self.pack_info is None:
            return
        try:
            root = self.pack_info.path
            if not recover_mod.has_leftovers(root):
                return
            stats = recover_mod.recover_leftovers(root, self.log.log)
            text = recover_mod.summary(stats)
            if text:
                self.log.log("warn", f"上次更新没有正常结束：{text}")
        except Exception as e:  # noqa: BLE001
            self.log.log("warn", f"自检残留失败：{e}")

    def _scan_worker(self):
        # 全程用局部快照：线程跑起来之后 self.* 可能被别的操作改动
        update_zip = self.update_zip
        pack_info = self.pack_info
        pack_kind = self.pack_kind
        if update_zip is None or pack_info is None:
            raise RuntimeError("更新包或整合包信息已丢失")
        assert update_zip is not None

        # 比对之前先把上次中断留下的残渣收拾干净：
        # 否则半个目录 / .pulses_new 会被当成"玩家本地文件"算进差异里
        self._recover_apply_leftovers()

        self._temp_dir = self._make_work_dir()
        if self._temp_dir is None:
            raise RuntimeError("无法创建临时工作目录")
        extract_dir = Path(self._temp_dir.name)

        self.after(0, self._set_hint, "正在解压更新包…")
        with zipfile.ZipFile(update_zip, "r") as zf:
            zf.extractall(extract_dir)

        content_root, is_fallback = locate_content_root(extract_dir)
        self._overrides_root = content_root
        # 老格式/裸 ZIP：内容根 = 解压根目录，包自身的元数据文件必须被排除
        # （否则会被当成更新内容写进玩家整合包根目录）
        self._overrides_fallback = is_fallback

        pack, err = parse_mrpack(update_zip)
        merged: list[dict] = []
        index_tops: set[str] = set()
        if err or pack is None:
            self._new_root = self._overrides_root
            folders = self._top_folders(self._overrides_root)
            file_items = self._root_file_items(self._overrides_root)
            mapping = {f: self._default_strategy_for_top(f, False, pack_kind)
                       for f in folders}
            mapping.update({f: self._default_strategy_for_top(f, True,
                                                            pack_kind)
                            for f in file_items})
            self.after(0, lambda m=mapping, fs=folders, fi=file_items:
                       self._apply_strategies(m, fs, fi))
        else:
            merged = merge_files(pack)
            index_tops = index_top_folders(pack)
            self._new_root = extract_dir
            folders = sorted(set(top_level_folders(merged)) | index_tops)
            file_items = self._root_file_items(self._overrides_root)
            mapping = {f: self._default_strategy_for_top(f, False, pack_kind)
                       for f in folders}
            mapping.update({f: self._default_strategy_for_top(f, True,
                                                            pack_kind)
                            for f in file_items})
            self.after(0, lambda m=mapping, fs=folders, fi=file_items:
                       self._apply_strategies(m, fs, fi))

        self._merged = merged

        user_wl: list[str] = []
        try:
            wl = eapack_mod.read_whitelist(update_zip)
            if wl:
                user_wl = list(wl)
        except Exception:  # noqa: BLE001, S110
            pass

        final_wl = sorted(set(user_wl) | index_tops)
        if not final_wl:
            final_wl = ["mods"]

        index_hashes: dict[str, dict] = {}
        for item in merged:
            p = item.get("path", "")
            if not p:
                continue
            h: dict[str, str] = {}
            if item.get("sha1"):
                h["sha1"] = item["sha1"]
            if item.get("sha256"):
                h["sha256"] = item["sha256"]
            if item.get("sha512"):
                h["sha512"] = item["sha512"]
            if h:
                index_hashes[p] = h

        if pack is not None:
            for f in pack.files:
                if not f.path or f.path in index_hashes:
                    continue
                h2: dict[str, str] = {}
                if f.sha1:
                    h2["sha1"] = f.sha1
                if f.sha256:
                    h2["sha256"] = f.sha256
                if f.sha512:
                    h2["sha512"] = f.sha512
                if h2:
                    index_hashes[f.path] = h2

        # 消费更新包自带的哈希清单（include_hashes / precompute_overrides）：
        #   files  → 补上 overrides 文件的期望哈希，省掉"读解压后的文件"
        #   folders→ 顶层目录的期望内容哈希，比较时只需算本地一侧
        folder_hashes: dict[str, str] = {}
        try:
            recorded = eapack_mod.read_hashes(update_zip)
            if isinstance(recorded, dict):
                for rel, entry in (recorded.get("files") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    algo = str(entry.get("algo", "") or "")
                    value = str(entry.get("value", "") or "")
                    if algo and value and rel not in index_hashes:
                        index_hashes[rel] = {algo: value}
                fh = recorded.get("folders")
                if isinstance(fh, dict):
                    folder_hashes = {str(k): str(v) for k, v in fh.items()
                                     if isinstance(v, str) and v}
        except Exception:  # noqa: BLE001, S110
            pass

        self._index_hashes = index_hashes
        compare_root = self._overrides_root
        self.after(0, lambda: self.progress.set(0.0, animate=False))

        def _progress(done: int, total: int, phase: str):
            if total <= 0:
                total = 1
            ratio = done / total
            label = f"比对  {done}/{total}"
            self.after(0, self._update_scan_progress, ratio, label)

        # 比对线程数走首选项（compare.hash_threads），不再硬编码
        try:
            hash_threads = int(
                db.get_compare_options().get("hash_threads", 16) or 16)
        except Exception:  # noqa: BLE001
            hash_threads = 16
        hash_threads = max(1, min(64, hash_threads))

        diff = diff_packs_parallel(
            pack_info.path, compare_root,
            whitelist=final_wl,
            index_hashes=index_hashes,
            threads=hash_threads, progress=_progress,
            folder_hashes=folder_hashes)
        self.diff = diff

    @staticmethod
    def _top_folders(root: Path) -> list[str]:
        if not root.is_dir():
            return []
        folders = set()
        try:
            for entry in root.iterdir():
                if entry.is_dir():
                    folders.add(entry.name)
        except OSError:
            pass
        return sorted(folders)

    @staticmethod
    def _root_file_items(root: Path) -> set[str]:
        items: set[str] = set()
        if not root.is_dir():
            return items
        try:
            for entry in root.iterdir():
                if entry.is_file() and not is_reserved_root_name(entry.name):
                    items.add(entry.name)
        except OSError:
            pass
        return items

    def _update_scan_progress(self, ratio: float, label: str):
        try:
            self.progress.set(max(0.0, min(1.0, ratio)), animate=True)
            self._set_hint(label)
        except Exception:  # noqa: BLE001, S110
            pass

    def _collect_download_tasks_from_diff(
            self, diff: DiffResult,
            merged: list[dict] | None,
            overrides_dir: Path | None,
            checked: dict | None = None,
            strategies: dict | None = None) -> list[DownloadTask]:
        if not merged:
            return []

        checked_map = dict(checked) if checked else {}
        strategies_map = dict(strategies) if strategies else {}

        def _is_checked(top: str) -> bool:
            # 统一走 apply_rules；空映射时按"全部需要"处理（尚未建表的早退路径）
            return _rule_is_checked(checked_map, top, default=True)

        needed: set[str] = set()
        for ch in diff.added + diff.modified:
            rel = ch.rel_path
            top = rel.parts[0] if rel.parts else ""
            if not _is_checked(top):
                continue
            strat = resolve_strategy(strategies_map, top)

            if ch.is_folder_level:
                prefix = rel.as_posix() + "/"
                for item in merged:
                    item_rel = item.get("path", "")
                    if item_rel.startswith(prefix):
                        needed.add(item_rel)
            else:
                if ch.kind.value == "modified" \
                        and strat == Strategy.SKIP_SAME:
                    continue
                needed.add(rel.as_posix())

        if not needed:
            return []

        if overrides_dir is None:
            return []

        tasks: list[DownloadTask] = []
        for item in merged:
            rel = item.get("path", "")
            if not rel or rel not in needed:
                continue
            target = overrides_dir / rel
            if target.is_file():
                continue
            urls = item.get("downloads") or []
            if not urls:
                continue
            tasks.append(DownloadTask(
                rel_path=rel,
                urls=list(urls),
                sha1=item.get("sha1", ""),
                sha256=item.get("sha256", ""),
                sha512=item.get("sha512", ""),
                file_size=int(item.get("file_size", 0) or 0),
            ))
        return tasks

    def _default_strategy_for(self, kind: PackKind) -> str:
        """
        非白名单顶层目录/根文件的默认策略。

        更新包（UPDATE）：目录级比对只能"合并"，默认必须是**替换重名**
        （同名覆盖、不删除）。默认"完全匹配"会在玩家本地目录里做
        rmtree + copytree，把玩家自己写的配置一并清掉。
        导出包（EXPORT）：保持历史语义（整包镜像）。
        """
        if kind == PackKind.EXPORT:
            return Strategy.FULL_MATCH.value
        return Strategy.REPLACE_SAME.value

    def _default_strategy_for_top(self, top: str, is_file: bool,
                                  kind: PackKind | None = None) -> str:
        """
        单个顶层项的默认策略（唯一事实来源：config.DEFAULT_STRATEGY_BY_DIR）。

        - 白名单内容文件夹（mods/resourcepacks/shaderpacks/tacz）走文件级
          比对，需要能删除"新版已移除"的条目 → 完全匹配；
        - config / saves / 其余目录是文件夹级比对 → 替换重名 / 跳过重名
          （合并覆盖，绝不删玩家自己写的文件）。
        """
        kind = kind if kind is not None else self.pack_kind
        if kind == PackKind.EXPORT:
            return Strategy.FULL_MATCH.value
        if is_file:
            # 根目录散文件（options.txt 等）：覆盖同名，但不删玩家文件
            return default_strategy_for_dir("").value
        return default_strategy_for_dir(top).value

    def _apply_strategies(self, mapping: dict,
                          folders: list[str],
                          file_items: set[str] | None = None):
        if file_items is None:
            file_items = set()

        if self.pack_kind == PackKind.UPDATE and self.update_zip:
            try:
                rec = eapack_mod.read_recommended_strategies(self.update_zip)
            except Exception:  # noqa: BLE001
                rec = {}
            for f in folders:
                if f in rec:
                    mapping[f] = rec[f]
            for f in file_items:
                if f in rec:
                    mapping[f] = rec[f]

        for f in list(folders) + list(file_items):
            if not mapping.get(f):
                # 兜底也必须是"不删玩家文件"的策略
                is_file = f in (file_items or set())
                mapping[f] = self._default_strategy_for_top(f, is_file)

        all_items = list(folders) + sorted(file_items)
        self.strategy_table.set_folders(all_items, file_items=file_items)
        self.strategy_table.set_strategies(mapping)
        # 基线 = 默认值 + 包内推荐值（未叠加玩家手改），用于判断"是否自定义过"
        self._baseline_strategies = {
            name: strat for name, strat in
            self.strategy_table.get_strategies().items()}
        self._refresh_ui_state()

    def _on_scan_done(self):
        self._phase = _PHASE_IDLE
        if self.diff is None:
            return

        if self._restore_resume_strategies():
            self.log.log("info", "已恢复你上次为这个更新包设置的策略")

        checked = self.strategy_table.get_checked()
        strategies = self.strategy_table.get_strategies()
        self._download_tasks = self._collect_download_tasks_from_diff(
            self.diff, self._merged, self._overrides_root,
            checked=checked, strategies=strategies)

        self.change_list.load(self.diff,
                              checked=checked,
                              strategies=strategies)
        self._take_strategy_snapshot()

        self.log.log("info",
                     f"比对完成：+{len(self.diff.added)} "
                     f"~{len(self.diff.modified)} "
                     f"-{len(self.diff.deleted)}")

        if self._resume_after_scan:
            self._resume_after_scan = False
            self.progress.set(1.0, animate=True)
            if self._download_tasks:
                self._set_hint(
                    f"比对完成，继续下载 "
                    f"{len(self._download_tasks)} 个文件")
                self._start_download_and_apply()
            else:
                self._set_hint("比对完成，直接应用更新")
                self._start_apply()
            return

        self._set_button_state(_BTN_CONFIRM)
        self.progress.set(1.0, animate=True)

        if self._download_tasks:
            self._set_hint(
                f"比对完成，需下载 {len(self._download_tasks)} 个文件，"
                f"点击「确认更新」开始")
        else:
            self._set_hint("比对完成，点击「确认更新」开始应用")

        self._refresh_ui_state()

    def _on_error(self, msg: str):
        self.log.log("error", msg)
        self._set_hint("比对失败")
        self._phase = _PHASE_IDLE
        self._set_button_state(_BTN_READY)
        self._refresh_ui_state()

    # ------------------------------------------------------------------
    # 阶段 2：下载
    # ------------------------------------------------------------------
    def _start_download_and_apply(self):
        if self._phase != _PHASE_IDLE:
            return
        if not self._download_tasks:
            self._start_apply()
            return

        cache_root = self._cache_root
        if cache_root is None:
            self.log.log("error", "未设置下载缓存目录，无法下载")
            return

        self._phase = _PHASE_DOWNLOAD
        self._abort_flag = False
        # 下载阶段就锁定：否则"处理中"还能拖入新包/清空整合包，
        # 而下载线程仍持有旧目录（会用错的包执行更新）
        self.app_state.lock()
        self._install_close_guard()

        started = time()
        self._phase_started_at = started
        self._set_button_state(_BTN_BUSY)
        self.progress.set_file_progress(0.0)
        self.progress.set(0.0, animate=False)

        self._speed_rel = ""
        self._speed_samples.clear()
        self._current_rel = ""
        self._done_count = 0
        self._total_count = len(self._download_tasks)
        self._active_files.clear()
        self._file_speed_samples.clear()
        self._byte_last_at.clear()
        try:
            ms = float(db.get_ui_options().get("progress_throttle_ms", 200))
        except Exception:  # noqa: BLE001
            ms = 200.0
        self._byte_throttle_s = max(0.0, min(2.0, ms / 1000.0))

        # 槽位面板按**磁盘策略限制后**的有效并发显示，而不是配置值
        try:
            from ..core.downloader import (
                _get_options,
                disk_policy_summary,
                engine_banner,
                resolve_disk_policy,
            )
            policy = resolve_disk_policy(cache_root, _get_options())
            eff = int(policy.get("multi_slots", 0) or 0)
            if eff > 0:
                self.slot_panel.set_slots(eff)
            self.log.log("info", engine_banner())
            self.log.log("info", disk_policy_summary(cache_root,
                                                     _get_options(),
                                                     with_engine=False))
        except Exception:  # noqa: BLE001, S110
            pass

        # 一开始就落一份续传记录：进程被强杀时也能续传，
        # 并把你这次用的策略一并存下来
        self._write_resume()

        self._pending_errors.clear()
        self._pending_ready = False
        self._give_up = False
        self._give_up_missing = []
        self._pending_auto_opened = False

        def _on_started(task: DownloadTask):
            self.after(0, self._handle_file_started, task.rel_path)

        def _on_done(task: DownloadTask):
            # on_file_done 只在成功时触发 → 这里就能准确回填"已完成"，
            # 中止关闭时写出的 resume 统计不会再偏低
            self._mark_completed(task.rel_path)
            self.after(0, self._handle_file_done, task.rel_path)

        def _on_failed(task: DownloadTask, error: str):
            self.after(0, self._handle_file_failed, task.rel_path,
                       task.urls, error)

        def _prog(done: int, total: int, name: str):
            self.after(0, lambda d=done, t=total:
                       self._update_download_progress(d, t))

        def _bytes(rel: str, received: int, total_bytes: int):
            self.after(0, lambda r=rel, rc=received, tb=total_bytes:
                       self._on_byte_progress(r, rc, tb))

        def _log(level: str, msg: str):
            self.after(0, self.log.log, level, msg)

        def _should_abort() -> bool:
            return self._abort_flag

        def worker():
            results = download_files(
                self._download_tasks,
                target_dir=cache_root,
                threads=None,
                progress=_prog,
                log=_log,
                byte_progress=_bytes,
                should_abort=_should_abort,
                on_file_started=_on_started,
                on_file_done=_on_done,
                on_file_failed=_on_failed)
            self._last_results = results
            # 兜底：万一某个实现没有回调成功事件，这里按结果再补一次
            for r in results:
                if r.ok:
                    self._mark_completed(r.task.rel_path)
            self.after(0, lambda r=results: self._on_download_done(r))

        self._download_thread = threading.Thread(target=worker, daemon=True)
        self._download_thread.start()

    # ------------------------------------------------------------------
    # 「更新受阻」界面：右栏提示条 + 补入子窗口
    # ------------------------------------------------------------------
    def _pending_cache_root(self) -> Path | None:
        """补入文件该放哪个缓存区。
        wait 模式 = 本次下载的缓存目录；complete 模式 = 凭证里记录的目录。"""
        if self._pending_mode == "complete":
            return self._record_cache_root
        return self._cache_root

    def _task_for(self, rel: str) -> DownloadTask | None:
        for t in self._download_tasks:
            if t.rel_path == rel:
                return t
        if self._pending_mode == "complete":
            for it in (self._record_marker or {}).get("missing", []):
                if str(it.get("rel", "")) == rel:
                    return DownloadTask(
                        rel_path=rel,
                        urls=list(it.get("urls") or []),
                        sha1=str(it.get("sha1", "") or ""),
                        sha256=str(it.get("sha256", "") or ""),
                        sha512=str(it.get("sha512", "") or ""),
                        file_size=int(it.get("size", 0) or 0))
        return None

    def _pending_items(self) -> list[dict]:
        """当前仍需要玩家补入的文件（界面与凭证共用同一份数据）。"""
        if self._pending_mode == "complete":
            return [dict(it) for it in
                    (self._record_marker or {}).get("missing", [])]
        items: list[dict] = []
        for rel in list(self._pending_errors):
            task = self._task_for(rel)
            if task is None:
                continue
            item = pending_logic.entry_from_task(task)
            item["error"] = self._pending_errors.get(rel, "")
            items.append(item)
        return items

    def _compute_missing(self) -> list[dict]:
        """
        「放弃」时要写进凭证的缺失清单：缓存里没有、整合包里也没有
        （或对不上哈希）的文件。这是当场算的，不依赖后续状态。
        """
        cache = self._cache_root
        pack_root = self.pack_info.path if self.pack_info else None
        out: list[dict] = []
        for task in self._download_tasks:
            item = pending_logic.entry_from_task(task)
            item["error"] = self._pending_errors.get(task.rel_path, "")
            try:
                if cache is not None and pending_logic.file_matches(
                        cache / task.rel_path, item):
                    continue
                if pack_root is not None and pending_logic.file_matches(
                        pack_root / task.rel_path, item):
                    continue
            except Exception:  # noqa: BLE001, S110
                pass
            out.append(item)
        return out

    def _ensure_banner(self) -> PendingBanner | None:
        if self.pending_banner is not None:
            return self.pending_banner
        try:
            self.pending_banner = PendingBanner(
                self.right,
                on_open=lambda: self._open_pending_window(),
                on_give_up=self._on_give_up,
                on_dismiss=self._on_dismiss_record,
                mode=self._pending_mode)
            self.pending_banner.grid(row=0, column=0, sticky="ew")
            self.change_list.grid_remove()
            self.placeholder.grid_remove()
        except Exception:  # noqa: BLE001
            self.pending_banner = None
        return self.pending_banner

    def _open_pending_window(self, auto: bool = False):
        items = self._pending_items()
        if not items:
            return
        if auto and self._pending_auto_opened:
            return
        try:
            win = self.pending_window
            exists = False
            if win is not None:
                try:
                    exists = bool(win.winfo_exists())
                except Exception:  # noqa: BLE001
                    exists = False
            if win is None or not exists:
                win = PendingWindow(
                    self.winfo_toplevel(),
                    mode=self._pending_mode,
                    items=items,
                    on_file_dropped=self._on_file_dropped,
                    on_retry=self._retry_one,
                    on_all_resolved=self._on_all_failures_resolved,
                    on_give_up=self._on_give_up,
                    on_dismiss=self._on_dismiss_record,
                    on_hide=self._on_pending_hide,
                    on_notice=self.log.log)
                self.pending_window = win
            else:
                self._sync_pending_window(items)
            win.show_window()
            self._pending_auto_opened = True
        except Exception as e:  # noqa: BLE001
            self.log.log("error", f"无法打开补入窗口：{e}")
            self.pending_window = None

    def _sync_pending_window(self, items: list[dict]):
        """只做增量同步：新增的补页、已解决的关页，保持玩家当前所在页。"""
        win = self.pending_window
        if win is None:
            return
        try:
            want = {str(it.get("rel", "")) for it in items}
            for rel in list(win.pending_rels()):
                if rel not in want:
                    win.resolve(rel, notify=False)
            for item in items:
                if str(item.get("rel", "")) not in win.pending_rels():
                    win.add_item(item)
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_pending_ui(self):
        items = self._pending_items()
        n = len(items)
        banner = self._ensure_banner()
        if banner is not None:
            try:
                banner.set_count(n, mode=self._pending_mode)
                if n:
                    banner.grid()
                else:
                    banner.grid_remove()
            except Exception:  # noqa: BLE001, S110
                pass
        if n:
            self._sync_pending_window(items)
            self.change_list.grid_remove()
            self.placeholder.grid_remove()
        else:
            self._teardown_pending_ui()

    def _teardown_pending_ui(self):
        win, self.pending_window = self.pending_window, None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        banner, self.pending_banner = self.pending_banner, None
        if banner is not None:
            try:
                banner.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        self._pending_auto_opened = False
        try:
            if self.diff is not None:
                self.change_list.grid()
                self.placeholder.grid_remove()
            else:
                self.change_list.grid_remove()
                self.placeholder.grid()
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_pending_hide(self):
        """子窗口被收起（不是放弃）：提示条继续亮着，随时可以再打开。"""
        self.log.log("info", "补入窗口已收起；随时可以点「打开补入窗口」继续")

    def _on_all_failures_resolved(self):
        """所有缺口都补齐了（子窗口回调）。"""
        if self._phase == _PHASE_APPLY:
            return
        if self._pending_mode == "complete":
            self._finish_complete_mode()
            return
        # 下载线程可能还在跑（别的文件）：等它结束再应用，
        # 否则合并源会从"写到一半"的缓存目录里取文件
        if self._phase == _PHASE_DOWNLOAD \
                and not self._join_download_thread(0.0):
            self._pending_ready = True
            self._pending_errors.clear()
            self._set_hint("已补齐，等待其余下载完成…")
            self._teardown_pending_ui()
            return
        self.log.log("info", "失败文件已全部补入并通过校验，继续应用更新")
        self._phase = _PHASE_IDLE
        self._teardown_pending_ui()
        self._set_hint("补入完成，开始应用更新…")
        self._start_apply()

    def _enter_blocked_state(self, pending: list[dict]):
        """下载结束仍有缺口 → 进入受阻态（不应用、不放弃，等玩家补入）。"""
        self._phase = _PHASE_MANUAL
        self._write_resume()
        n = len(pending)
        self.log.log("warn", f"{n} 个文件所有下载源都失败了，需要手动补入")
        self._set_hint(f"更新受阻：还差 {n} 个文件", color=Color.WARNING)
        self._ensure_banner()
        self._pending_auto_opened = False
        self._open_pending_window(auto=True)
        self._refresh_pending_ui()
        self._refresh_ui_state()

    def _on_give_up(self):
        if self._phase == _PHASE_APPLY:
            return
        pending = self._pending_items()
        if not pending:
            return
        names = "\n".join("· " + str(it.get("rel", ""))
                          for it in pending[:10])
        if len(pending) > 10:
            names += f"\n…… 还有 {len(pending) - 10} 个"
        confirm(
            self.winfo_toplevel(),
            "放弃补齐？",
            f"还有 {len(pending)} 个文件没有补入：\n\n{names}\n\n"
            f"放弃后软件会**立刻完成本次更新**，但整合包里会缺少这些文件。\n"
            f"软件会在整合包内写入「!!!更新未完成-请阅读!!!.txt」与给程序看的"
            f"记录文件；下次定位这个整合包时会提醒你补齐。\n\n"
            f"其余仍在下载的文件会继续下完。确定放弃吗？",
            confirm_text="放弃并完成更新",
            cancel_text="继续补齐",
            level="error",
            danger=True,
            on_result=self._do_give_up)

    def _do_give_up(self, ok: bool):
        if not ok:
            return
        self._give_up = True
        self._give_up_missing = self._compute_missing()
        n = len(self._give_up_missing)
        self.log.log("warn", f"已选择放弃补齐（{n} 个文件将记为缺失），"
                             f"其余下载会继续完成")
        self._teardown_pending_ui()
        if self._phase == _PHASE_DOWNLOAD \
                and not self._join_download_thread(0.0):
            # 还有文件在下：等下载收尾，由 _on_download_done 接手应用
            self._set_hint("正在等待其余下载完成…", color=Color.WARNING)
            return
        self._phase = _PHASE_IDLE
        self._set_hint("已放弃补齐，开始应用更新…", color=Color.WARNING)
        self._start_apply()

    def _on_dismiss_record(self):
        """补齐模式里点「不再提醒」：删掉整合包里的凭证。"""
        pack_root = self.pack_info.path if self.pack_info else None
        if pack_root is not None:
            pending_logic.clear_marker(pack_root)
            self.log.log("info", "已删除整合包里的「更新未完成」提示文件")
        self._pending_mode = "wait"
        self._record_marker = None
        self._record_cache_root = None
        self._record_rels = []
        self._pending_errors.clear()
        self._teardown_pending_ui()
        self._set_hint("已跳过补齐（记录已删除）")
        self._refresh_ui_state()

    # ------------------------------------------------------------------
    # 重新下载单个文件 / 补齐模式
    # ------------------------------------------------------------------
    def _retry_one(self, rel: str, report):
        task = self._task_for(rel)
        cache = self._pending_cache_root()
        if task is None or cache is None:
            try:
                report("failed", "缺少这个文件的下载信息")
            except Exception:  # noqa: BLE001, S110
                pass
            return
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001, S110
            pass

        def worker():
            ok, msg = False, ""
            try:
                res = download_files([task], target_dir=cache, threads=1,
                                     should_abort=lambda: self._abort_flag)
                if res and res[0].ok:
                    ok = True
                else:
                    msg = (res[0].error if res else "") or "仍然连不上"
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
            self.after(0, lambda o=ok, m=msg:
                       self._on_retry_done(rel, o, m, report))

        threading.Thread(target=worker, daemon=True).start()

    def _on_retry_done(self, rel: str, ok: bool, msg: str, report):
        if ok:
            self._mark_completed(rel)
            self._pending_errors.pop(rel, None)
            self.log.log("info", f"重新下载成功：{rel}")
        else:
            self.log.log("warn", f"重新下载失败：{rel}（{msg}）")
        try:
            report("ok" if ok else "failed", msg)
        except Exception:  # noqa: BLE001, S110
            pass
        self._refresh_pending_ui()

    def _check_pending_record(self):
        """
        定位整合包后核对"上次更新未完成"的凭证。
        只在干净状态（没有装更新包、没有比对结果）时询问，避免打断正在进行的流程。
        """
        if self._phase != _PHASE_IDLE or self.pack_info is None:
            return
        if self.update_zip is not None or self.diff is not None:
            return
        try:
            res = pending_logic.check_marker(self.pack_info.path)
        except Exception:  # noqa: BLE001
            return
        if not res.get("has"):
            return
        if res.get("resolved"):
            pending_logic.clear_marker(self.pack_info.path)
            self.log.log("info", "上次更新缺少的文件已经补齐，"
                                 "已清理整合包里的提示文件")
            return

        marker = res["marker"] or {}
        outstanding = res["outstanding"] or []
        n = len(outstanding)
        names = "\n".join("· " + str(it.get("rel", ""))
                          for it in outstanding[:10])
        if n > 10:
            names += f"\n…… 还有 {n - 10} 个"
        state = {"no_remind": False}

        def _on_checkbox(val: bool):
            state["no_remind"] = bool(val)

        def _on_result(ok: bool):
            if ok:
                self._enter_complete_mode(marker, outstanding)
            elif state["no_remind"]:
                self._on_dismiss_record()

        self.log.log("warn", f"这个整合包上次更新还差 {n} 个文件没有补入")
        confirm(
            self.winfo_toplevel(),
            "上次更新未完成",
            f"整合包「{marker.get('pack_name') or self.pack_info.name}」"
            f"上次更新还差 {n} 个文件：\n\n{names}\n\n"
            f"现在补齐吗？（补齐后本次更新才算完整）",
            confirm_text="现在补齐",
            cancel_text="稍后",
            level="warn",
            on_result=_on_result,
            extra_checkbox=("不再提醒", _on_checkbox))

    def _enter_complete_mode(self, marker: dict, outstanding: list[dict]):
        self._pending_mode = "complete"
        self._record_marker = dict(marker)
        self._record_marker["missing"] = [dict(it) for it in outstanding]
        self._record_rels = [str(it.get("rel", "")) for it in outstanding]
        cache = str(marker.get("cache_root", "") or "")
        root = Path(cache) if cache else None
        if root is None:
            root = self._resolve_cache_root(Path(marker.get("update_zip", ""))
                                            if marker.get("update_zip")
                                            else Path("pending"))
        if root is not None:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001, S110
                pass
        self._record_cache_root = root
        self._pending_errors = {str(it.get("rel", "")): "上次更新未完成"
                                for it in outstanding}
        self.log.log("warn", f"上次更新还差 {len(outstanding)} 个文件，"
                             f"补齐后本次更新才完整")
        self._ensure_banner()
        self._pending_auto_opened = False
        self._open_pending_window(auto=True)
        self._refresh_pending_ui()
        self._set_hint(f"补齐上次更新：还差 {len(outstanding)} 个文件",
                       color=Color.WARNING)
        self._refresh_ui_state()

    def _finish_complete_mode(self):
        """补齐完成：用只含这些文件的补丁计划走同一套 execute_plan。"""
        if self.pack_info is None:
            return
        rels = [Path(r) for r in self._record_rels if r]
        cache = self._record_cache_root
        pack_root = self.pack_info.path
        if not rels or cache is None:
            return
        self._phase = _PHASE_APPLY
        self.app_state.lock()
        self._install_close_guard()
        self._set_button_state(_BTN_BUSY)
        self._set_hint("正在补齐上次更新的文件…")
        self.log.log("info", f"开始补齐 {len(rels)} 个文件…")

        def _log(level: str, msg: str):
            self.after(0, self.log.log, level, msg)

        source = PlanSource([SourceLayer(cache, consume=False)])
        options = self._transfer_options()

        def worker():
            try:
                plan = UpdatePlan(copy=list(rels))
                updater_fill(plan, source, self._hash_for)
                report = execute_plan(plan, pack_root, source=source,
                                      log=_log,
                                      **self._execute_options(options))
                vstats: dict = {}
                failed = verify_after_update(
                    plan, pack_root, deep=bool(options.get("deep_verify")),
                    stats=vstats)
                cp_mod.mark_applied(pack_root)
                self.after(0, lambda r=report, f=failed:
                           self._on_complete_done(r, f))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.after(0, lambda m=msg: self._on_complete_error(m))

        threading.Thread(target=worker, daemon=True).start()

    def _on_complete_done(self, report: dict, verify_failed: list):
        problems = len(report.get("failed", [])) + len(verify_failed)
        pack_root = self.pack_info.path if self.pack_info else None
        if problems:
            self.log.log("error", f"补齐完成，但有 {problems} 项失败")
            for item in (report.get("failed", []) + verify_failed)[:10]:
                self.log.log("error",
                             f"失败：{item.get('rel')} - {item.get('error')}")
            self._set_hint(f"补齐未完全成功（{problems} 项失败）",
                           color=Color.WARNING)
        else:
            self.log.log("info", "上次更新已补齐，缺失文件已全部就位")
            self._set_hint("上次更新已补齐 ✓")
            if pack_root is not None:
                pending_logic.clear_marker(pack_root)
                self.log.log("info", "已清理整合包里的「更新未完成」提示文件")
        self._pending_mode = "wait"
        self._record_marker = None
        self._record_cache_root = None
        self._record_rels = []
        self._pending_errors.clear()
        self._phase = _PHASE_IDLE
        self.app_state.unlock()
        self._uninstall_close_guard()
        self._teardown_pending_ui()
        self._set_button_state(_BTN_CONFIRM)
        self._refresh_ui_state()

    def _on_complete_error(self, msg: str):
        self.log.log("error", f"补齐失败：{msg}")
        self._set_hint("补齐失败", color=Color.ERROR)
        self._phase = _PHASE_IDLE
        self.app_state.unlock()
        self._uninstall_close_guard()
        self._set_button_state(_BTN_CONFIRM)
        self._refresh_ui_state()

    def _mark_completed(self, rel: str):
        """记录"这个文件确实下好了"（线程安全：list.append 是原子的）。"""
        if rel and rel not in self._completed_files:
            self._completed_files.append(rel)

    def _handle_file_started(self, rel: str):
        self._current_rel = rel
        if rel != self._speed_rel:
            self._speed_rel = rel
            self._speed_samples.clear()
        self._active_files[rel] = {"received": 0, "total": 0}
        self._file_speed_samples[rel] = deque(maxlen=6)
        self._render_hint()

    def _handle_file_failed(self, rel: str, urls: list[str], error: str):
        self._active_files.pop(rel, None)
        self._file_speed_samples.pop(rel, None)
        self._pending_errors[rel] = str(error or "")
        self.log.log("warn", f"下载失败，需要手动补入：{rel}")
        if self._give_up or self._pending_ready:
            return          # 已经决定放弃/已补齐，只记录，不再打扰
        self._ensure_banner()
        self._open_pending_window(auto=True)
        self._refresh_pending_ui()

    def _handle_file_done(self, rel: str):
        self._active_files.pop(rel, None)
        self._file_speed_samples.pop(rel, None)
        self._refresh_slots()

    def _update_download_progress(self, done: int, total: int):
        self._done_count = done
        self._total_count = total
        self.progress.set(done / total if total else 0, animate=True)
        self._render_hint()

    def _on_byte_progress(self, rel_path: str, received: int,
                          total_bytes: int):
        now = time()
        # 节流：同一文件在 progress_throttle_ms 内的中间进度直接丢掉，
        # 但最后一个字节（received >= total）必须放行，保证进度条能到 100%
        done_all = total_bytes > 0 and received >= total_bytes
        if not done_all and self._byte_throttle_s > 0:
            last = self._byte_last_at.get(rel_path, 0.0)
            if now - last < self._byte_throttle_s:
                return
        self._byte_last_at[rel_path] = now

        if rel_path != self._speed_rel:
            self._speed_rel = rel_path
            self._speed_samples.clear()
        self._speed_samples.append((now, received))

        info = self._active_files.get(rel_path)
        if info is None:
            info = {"received": 0, "total": 0}
            self._active_files[rel_path] = info
        info["received"] = received
        info["total"] = total_bytes

        samples = self._file_speed_samples.get(rel_path)
        if samples is None:
            samples = deque(maxlen=6)
            self._file_speed_samples[rel_path] = samples
        samples.append((now, received))

        if total_bytes > 0:
            self.progress.set_file_progress(received / total_bytes)
        else:
            self.progress.set_file_progress(
                min(0.95, (self.progress.get_file_progress() or 0.0) + 0.02))

        self._render_hint()

        if self._slot_expanded:
            self._schedule_slot_refresh()

    def _render_hint(self):
        if self._phase != _PHASE_DOWNLOAD:
            return
        rel = self._current_rel
        speed = self._current_speed_text()
        done = self._done_count
        total = self._total_count

        name = _shorten_path(rel, 60) if rel else ""
        parts: list[str] = []
        if name:
            parts.append(f"正在下载：{name}")
        else:
            parts.append("正在下载…")
        if speed:
            parts.append(speed)
        if total > 0:
            parts.append(f"{done}/{total}")
        self._set_hint("  ·  ".join(parts))

    def _current_speed_text(self) -> str:
        if len(self._speed_samples) < 2:
            return ""
        t0, b0 = self._speed_samples[0]
        t1, b1 = self._speed_samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return ""
        delta = b1 - b0
        if delta <= 0:
            return "0 B/s"
        bps = delta / dt
        return _fmt_speed(bps)

    def _on_download_done(self, results):
        self.progress.set_file_progress(None)

        # 已经进入应用阶段（例如"放弃"的等待线程先一步接手）：
        # 这里不能再改 phase / 按钮 / 提示
        if self._phase == _PHASE_APPLY:
            return

        failed = [r for r in results if not r.ok and not r.aborted]
        aborted = any(r.aborted for r in results)

        if aborted:
            self.log.log("warn", "下载已中止")
            if self._give_up:
                self._set_hint("正在停止剩余下载…")
                return
            self._phase = _PHASE_IDLE
            self._set_hint("已中止下载")
            self._uninstall_close_guard()
            self.app_state.unlock()
            self._teardown_pending_ui()
            self._set_button_state(_BTN_CONFIRM)
            self._refresh_ui_state()
            return

        self.log.log("info",
                     f"下载完成：成功 {len(results) - len(failed)}，"
                     f"失败 {len(failed)}")

        pending = self._pending_items()
        if self._give_up or self._pending_ready or not pending:
            # 缺口在下载期间被补齐 / 用户已放弃 / 本来就没有缺口 → 继续应用
            self._pending_ready = False
            self._phase = _PHASE_IDLE
            self._teardown_pending_ui()
            if self._give_up:
                self._set_hint("已放弃补齐，开始应用更新…", color=Color.WARNING)
            else:
                self._set_hint("下载完成，开始应用更新…")
            self._start_apply()
            return

        # 仍有下不来的文件 → 进入受阻态，等玩家补入
        self._enter_blocked_state(pending)

    def _on_file_dropped(self, rel_path: str, file_path: Path) -> bool:
        task = self._task_for(rel_path)
        cache = self._pending_cache_root()
        if task is None or cache is None:
            return False

        target = cache / rel_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)
        except Exception as e:  # noqa: BLE001
            self.log.log("error", f"投入失败：{e}")
            return False

        from ..core.downloader import _verify
        ok, err = _verify(target, task)
        if not ok:
            self.log.log("error", f"{rel_path} 校验不通过：{err}")
            try:
                target.unlink()
            except Exception:  # noqa: BLE001, S110
                pass
            return False

        self.log.log("info", f"{rel_path} 校验通过")
        self._mark_completed(rel_path)
        self._pending_errors.pop(rel_path, None)
        return True

    def _join_download_thread(self, timeout: float = 5.0) -> bool:
        """等下载线程结束。返回是否确认已结束。"""
        t = self._download_thread
        if t is None or not t.is_alive():
            return True
        t.join(timeout=timeout)
        return not t.is_alive()

    # ------------------------------------------------------------------
    # 阶段 3：应用
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 搬运参数（首选项 ui.apply_*）：分块大小 / 批次大小 / 批次暂停
    # ------------------------------------------------------------------
    @staticmethod
    def _transfer_options() -> dict:
        """
        控制"搬运时不要吃满硬盘"的参数。

        默认值针对 Windows 机械盘/固态盘混合场景：4MiB 一块、每 96MiB
        让出 12ms。对同卷"移动"完全没有影响（本来就不产生 IO），
        只有真的跨盘复制时才会生效，代价约 0.01%。
        """
        try:
            opts = db.get_ui_options()
        except Exception:  # noqa: BLE001
            opts = {}

        def _num(key: str, default: float) -> float:
            try:
                return float(opts.get(key, default))
            except Exception:  # noqa: BLE001
                return default

        return {
            "chunk_bytes": max(
                256 * 1024, int(_num("apply_chunk_kb", 4096)) * 1024),
            "pace_ms": max(0.0, min(50.0, _num("apply_chunk_pause_ms", 0))),
            "batch_bytes": max(
                4 * 1024 * 1024, int(_num("apply_batch_mb", 96)) * 1024 * 1024),
            "batch_pause_ms": max(
                0.0, min(200.0, _num("apply_batch_pause_ms", 12))),
            "space_check": bool(int(_num("apply_space_check", 1))),
            "deep_verify": bool(int(_num("apply_deep_verify", 1))),
            "clean_cache": bool(int(_num("apply_clean_cache", 1))),
        }

    @staticmethod
    def _execute_options(options: dict) -> dict:
        """挑出 execute_plan 认的参数（deep_verify/clean_cache 是给这里用的）。"""
        return {k: v for k, v in options.items()
                if k not in ("deep_verify", "clean_cache")}

    def _hash_for(self, rel_key: str) -> str:
        """索引里这个文件的期望摘要（"sha1:...."），没有就返回空。"""
        entry = self._index_hashes.get(rel_key) or {}
        for algo in ("sha1", "sha256", "sha512"):
            value = entry.get(algo)
            if value:
                return f"{algo}:{value}"
        return ""

    def _make_plan_source(self) -> PlanSource | None:
        """
        本次更新的内容来源：**下载缓存优先，其次解压出来的 overrides**。

        和旧实现的区别：不再先把整包复制成 `_merged` 副本（1GB 内容要
        多写 1GB、还占着 UI 线程），而是按需取——计划里要哪个文件就
        搬哪个。缓存层保留源文件（方便重试/续传），overrides 是临时
        解压产物，搬运后直接消耗掉。
        """
        layers: list[SourceLayer] = []
        if self._cache_root is not None:
            allowed = {t.rel_path for t in self._download_tasks}
            allowed.update(self._completed_files)
            layers.append(SourceLayer(self._cache_root, allowed=allowed,
                                      consume=False))
        if self._overrides_root is not None:
            fallback = bool(self._overrides_fallback)

            def _ignore(rel: Path, _fb: bool = fallback) -> bool:
                # 老格式回退（内容根 = 解压根目录）时，包自身的元数据
                # 文件绝不能进入应用源
                return bool(_fb and len(rel.parts) == 1
                            and is_reserved_root_name(rel.name))

            layers.append(SourceLayer(self._overrides_root, ignore=_ignore,
                                      consume=True))
        if not layers:
            return None
        return PlanSource(layers)

    def _start_apply(self):
        # 并发保护：同一时刻只允许一个 execute_plan 在写整合包
        if self._phase == _PHASE_APPLY:
            return
        if self._phase == _PHASE_DOWNLOAD:
            # 下载还没收尾，等 _on_download_done 接手
            return
        if (self.diff is None or self.pack_info is None
                or self._overrides_root is None):
            return

        source = self._make_plan_source()
        if source is None:
            self.log.log("error", "无法构建更新源")
            return

        # Tk 控件只能在主线程读：先把策略/勾选取出来再进后台线程
        strategies = self.strategy_table.get_strategies()
        checked = self.strategy_table.get_checked()
        old_root = self.pack_info.path
        options = self._transfer_options()

        self._phase = _PHASE_APPLY
        self.app_state.lock()
        # 关闭守卫必须在这里装：所有进入应用阶段的路径（含
        # "无需下载直接应用"和 resume 直通）都要有强关警告
        self._install_close_guard()
        self._set_button_state(_BTN_BUSY)
        self.progress.set_file_progress(None)
        self.progress.set(0.0, animate=False)
        self._set_hint("正在应用更新…")

        def _log(level: str, msg: str) -> None:
            self.after(0, self.log.log, level, msg)

        def _progress(done: int, total: int) -> None:
            self.after(0, self._set_progress, done, total)

        def worker():
            # 生成计划、搬运、校验全部在后台线程：主线程一步都不能卡。
            # （旧实现会在 UI 线程上先把整包复制成 _merged 副本，
            #   1GB 的包能把窗口冻住十几秒。）
            try:
                plan = build_plan(self.diff, checked, strategies,
                                  old_root=old_root, source=source,
                                  hash_for=self._hash_for)
                self.plan = plan
                exec_opts = self._execute_options(options)
                report = execute_plan(plan, old_root, source=source,
                                      log=_log, progress=_progress,
                                      **exec_opts)
                vstats: dict = {}
                failed = verify_after_update(
                    plan, old_root, deep=bool(options.get("deep_verify")),
                    stats=vstats)
                if vstats.get("deep_files"):
                    _log("info", f"内容级校验：{vstats['deep_files']} 个文件 / "
                                 f"{transfer.human_bytes(vstats['deep_bytes'])}"
                                 f" 全部一致")
                # 校验也跑完了才写 applied：如果进程死在上面这两步之间，
                # 检查点会停在 verify，下次启动能发现"上次收尾没做完"
                cp_mod.mark_applied(old_root)
                self.after(0, lambda r=report, f=failed:
                           self._on_apply_done(r, f))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.after(0, lambda m=msg: self._on_apply_error(m))

        threading.Thread(target=worker, daemon=True).start()

    def _set_progress(self, done: int, total: int):
        self.progress.set(done / total if total else 1.0, animate=True)
        self._set_hint(f"应用 {done}/{total}")

    def _on_apply_done(self, report: dict, verify_failed: list):
        self.progress.set(1.0, animate=True)

        problems = len(report["failed"]) + len(verify_failed)
        if problems:
            # 有失败就别说"更新完成 ✓"
            self._set_hint(f"更新已应用，但有 {problems} 项未成功（见日志）",
                           color=Color.WARNING)
        else:
            self._set_hint("更新完成 ✓")

        moved = int(report.get("moved", 0) or 0)
        copied = int(report.get("bytes", 0) or 0)
        self.log.log("info",
                     f"更新完成：新增 {report['applied']}，"
                     f"删除 {report['deleted']}，"
                     f"失败 {len(report['failed'])}"
                     f"（瞬时搬运 {moved} 个文件，"
                     f"实际复制 {transfer.human_bytes(copied)}）")

        if report["failed"] or verify_failed:
            for item in report["failed"][:20]:
                self.log.log("error",
                             f"失败：{item['rel']} - {item['error']}")
            for item in verify_failed[:20]:
                self.log.log("error",
                             f"校验失败：{item['rel']} - {item['error']}")

        if self._give_up:
            # 玩家显式放弃补齐：写入"更新未完成"凭证（人看的 txt + 程序看的 json）
            self._write_pending_record()
            if self._cache_root is not None:
                resume_mod.clear_resume(self._cache_root)
            self._give_up = False
        elif not problems:
            # 本次更新彻底完成：清掉续传记录，避免下次启动又弹"继续上次更新"
            if self._cache_root is not None:
                resume_mod.clear_resume(self._cache_root)
        elif self._cache_root is not None:
            self.log.log("info", "未完成的项目已保留续传记录，"
                                 "下次启动可继续")

        self._phase = _PHASE_IDLE
        self.app_state.unlock()
        self._uninstall_close_guard()
        if not problems and not self._give_up:
            self._release_download_cache()
            # 更新彻底成功 → 复位界面：旧变更列表已经过期，按钮还能再点
            # 一次"确认更新"的话会拿旧计划重跑一遍（白搬一次）。
            self._reset_after_apply()
        else:
            self._set_button_state(_BTN_CONFIRM)
        self._refresh_ui_state()

    def _release_download_cache(self):
        """更新彻底成功 → 释放本次下载缓存（可关）。"""
        try:
            if not self._transfer_options().get("clean_cache"):
                return
            root = self._cache_root
            if root is None:
                return
            ok, freed = cache_mod.release_pack_cache(root)
            if ok and freed:
                self.log.log(
                    "info", f"已释放本次下载缓存 "
                            f"{transfer.human_bytes(freed)}")
                self._freed_cache_bytes = freed
        except Exception as e:  # noqa: BLE001
            self.log.log("warn", f"释放下载缓存失败：{e}")

    def _reset_after_apply(self):
        """
        应用完成后的状态复位。

        旧实现什么都不做：变更列表还显示"~120 个文件待更新"，
        按钮仍是「确认更新」，再点一次就会用同一份旧计划再搬一遍。
        """
        self.plan = None
        self.diff = None
        self._merged = []
        self._download_tasks = []
        self._completed_files = []
        try:
            self.change_list.load(None)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.strategy_table.set_folders([])
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self._teardown_pending_ui()
        except Exception:  # noqa: BLE001, S110
            pass
        freed = int(getattr(self, "_freed_cache_bytes", 0) or 0)
        self._freed_cache_bytes = 0
        tail = (f"，已释放下载缓存 {transfer.human_bytes(freed)}"
                if freed else "")
        self._set_button_state(_BTN_READY)
        self._set_placeholder("本次更新已全部应用 ✓\n"
                              "（要再次核对可以点「开始更新」重新比对）")
        self._set_hint(f"更新完成 ✓ 本次更新已全部应用{tail}"
                       f"（要再次核对可以点「开始更新」重新比对）")

    def _set_placeholder(self, text: str):
        self._placeholder_text = text
        try:
            self.placeholder_label.configure(text=text)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_apply_error(self, msg: str):
        self.log.log("error", f"更新失败：{msg}")
        short = str(msg).strip()
        if len(short) > 60:
            short = short[:57] + "…"
        self._set_hint(f"更新失败：{short}" if short else "更新失败",
                       color=Color.ERROR)
        self._phase = _PHASE_IDLE
        self.app_state.unlock()
        self._uninstall_close_guard()
        self._set_button_state(_BTN_CONFIRM)

    # ------------------------------------------------------------------
    # 关闭守卫
    # ------------------------------------------------------------------
    def _install_close_guard(self):
        try:
            top = self.winfo_toplevel()
            installer = getattr(top, "install_close_guard", None)
            if callable(installer):
                # 主窗口支持叠加守卫（开发者端导出时会拒绝关闭）
                installer("player", self._on_close_request)
            else:
                top.protocol("WM_DELETE_WINDOW", self._on_close_request)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.sidebar.set_locked(True)
        except Exception:  # noqa: BLE001, S110
            pass

    def _uninstall_close_guard(self):
        try:
            top = self.winfo_toplevel()
            releaser = getattr(top, "release_close_guard", None)
            if callable(releaser):
                releaser("player")
            else:
                top.protocol("WM_DELETE_WINDOW", top.destroy)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.sidebar.set_locked(False)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_close_request(self):
        if self._phase == _PHASE_DOWNLOAD:
            confirm(
                self.winfo_toplevel(),
                "确认关闭？",
                "下载正在进行中。\n\n"
                "关闭后当前未下完的文件会被丢弃，"
                "但已完成的部分会保留。\n"
                "下次启动程序会询问是否继续更新。",
                confirm_text="关闭",
                cancel_text="继续下载",
                level="warn",
                danger=True,
                on_result=self._handle_close_download)
        elif self._phase == _PHASE_MANUAL:
            confirm(
                self.winfo_toplevel(),
                "放弃本次更新？",
                "还有文件没有下完，正在等待你手动补入。\n\n"
                "关闭将放弃本次更新（已下载的部分会保留，"
                "下次启动会询问是否继续）。",
                confirm_text="关闭",
                cancel_text="继续补入",
                level="warn",
                danger=True,
                on_result=self._handle_close_download)
        elif self._phase == _PHASE_APPLY:
            confirm(
                self.winfo_toplevel(),
                "强制关闭？",
                "更新正在写入整合包，强制关闭可能导致整合包损坏！\n\n"
                "建议等待更新完成。仍要关闭吗？",
                confirm_text="强制关闭",
                cancel_text="继续等待",
                level="error",
                danger=True,
                on_result=self._handle_close_apply)
        else:
            try:
                self.winfo_toplevel().destroy()
            except Exception:  # noqa: BLE001, S110
                pass

    def _handle_close_download(self, ok: bool):
        if not ok:
            return
        self._abort_flag = True
        self.log.log("warn", "正在中止下载…")
        self._set_hint("正在中止下载…")

        def _wait_and_close():
            t = self._download_thread
            stopped = True
            if t is not None and t.is_alive():
                t.join(timeout=5.0)
                stopped = not t.is_alive()
            # 线程已结束（或返回了部分结果）时，把成功的结果补进
            # _completed_files，避免 resume 里的"已完成"统计偏低
            for r in list(self._last_results or []):
                try:
                    if r.ok:
                        self._mark_completed(r.task.rel_path)
                except Exception:  # noqa: BLE001, S110
                    continue
            if self._cache_root:
                # 线程还没停就清理 .part 会和它抢文件：可能导致正在写的
                # 分片被删掉，随后判为失败。只有确认停下才清理。
                if stopped:
                    resume_mod.cleanup_stale_parts(self._cache_root)
                else:
                    try:
                        self.after(0, self.log.log, "warn",
                                   "下载线程未在 5 秒内结束，"
                                   "已跳过残留分片清理")
                    except Exception:  # noqa: BLE001, S110
                        pass
                self._write_resume()
            self.after(0, self._force_destroy)

        threading.Thread(target=_wait_and_close, daemon=True).start()

    def _handle_close_apply(self, ok: bool):
        if not ok:
            return
        self.after(0, self._force_destroy)

    def _force_destroy(self):
        # 退出前清掉解压副本，避免留下 GB 级残留
        win, self.pending_window = self.pending_window, None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        self._cleanup_temp_dir()
        try:
            top = self.winfo_toplevel()
            top.protocol("WM_DELETE_WINDOW", top.destroy)
            top.destroy()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _write_pending_record(self):
        """把"还缺哪些文件"写进整合包：!!!更新未完成-请阅读!!!.txt + json。"""
        pack_root = self.pack_info.path if self.pack_info else None
        if pack_root is None:
            return
        missing = list(self._give_up_missing or [])
        if not missing:
            missing = self._compute_missing()
        if not missing:
            self.log.log("info", "没有实际缺失的文件，不写「更新未完成」提示")
            return
        zip_path = str(self.update_zip) if self.update_zip else ""
        try:
            fingerprint = (resume_mod.quick_fingerprint(self.update_zip)
                           if self.update_zip else "")
        except Exception:  # noqa: BLE001
            fingerprint = ""
        marker = pending_logic.build_marker(
            pack_root,
            pack_name=self.pack_info.name if self.pack_info else "",
            pack_version=self.pack_info.version if self.pack_info else "",
            update_zip=zip_path,
            update_fingerprint=fingerprint,
            cache_root=str(self._cache_root or ""),
            missing=missing)
        if pending_logic.write_marker(pack_root, marker):
            self.log.log("warn",
                         f"已在整合包里写入「{pending_logic.NOTICE_FILENAME}」"
                         f"（{len(missing)} 个文件缺失），"
                         f"下次定位本整合包时会提醒补齐")
            self.log.log("info", f"提示文件位置："
                                 f"{pending_logic.notice_path(pack_root)}")
        else:
            self.log.log("error", "写入「更新未完成」提示文件失败，"
                                 "请自行记录缺失文件")

    # ------------------------------------------------------------------
    def _write_resume(self):
        if self._cache_root is None or self.update_zip is None:
            return
        try:
            pending = [
                t.rel_path for t in self._download_tasks
                if t.rel_path not in self._completed_files
            ]
            pack_name = self.pack_info.name if self.pack_info else ""
            version = self.pack_info.version if self.pack_info else ""
            pack_root = self.pack_info.path if self.pack_info else None
            resume_mod.write_resume(
                self._cache_root, self.update_zip,
                pack_root,
                completed=list(self._completed_files),
                failed=list(pending),
                total=len(self._download_tasks),
                started_at=self._phase_started_at or time(),
                pack_name=pack_name, version=version,
                strategies=self.strategy_table.get_strategies(),
                checked=self.strategy_table.get_checked(),
                strategies_customized=self._is_strategy_customized())
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _show_changelog(self):
        if not self._changelog_md:
            return
        from ..core.markdown_renderer import (
            open_preview_window,
            plain_text_html,
            wrap_html,
        )
        if self._changelog_html:
            full = wrap_html(self._changelog_html, light=True)
            open_preview_window(full)
            return
        open_preview_window(plain_text_html(self._changelog_md))

    # ------------------------------------------------------------------
    def _on_log_clear(self):
        self.log.clear()
        if self.pack_info is not None:
            self.log.log("info",
                         f"整合包：{self.pack_info.name}"
                         f"（{self.pack_info.mod_count} 个模组）")
        if self.update_zip is not None:
            kind_label = ("更新包" if self.pack_kind == PackKind.UPDATE
                          else "导出包")
            self.log.log("info",
                         f"{kind_label}：{self.update_zip.name}")

    # ------------------------------------------------------------------
    def _reset_pack_state(self):
        # 防御：任何清理都必须先让下载线程停下来，否则它会继续往被清空的
        # 缓存/解压目录里写文件
        self._abort_flag = True
        if not self._join_download_thread(timeout=5.0):
            self.log.log("warn", "下载线程尚未结束，稍后可能仍有文件落盘")
        self.update_zip = None
        self.pack_kind = PackKind.UNKNOWN
        self.diff = None
        self._merged = []
        self._index_hashes = {}
        self.plan = None
        self._new_root = None
        self._overrides_root = None
        self._cache_root = None
        self._changelog_md = ""
        self._changelog_html = ""
        self._changelog_rendering = False
        self._changelog_render_token += 1
        self._download_tasks = []
        self._completed_files = []
        self._speed_rel = ""
        self._speed_samples.clear()
        self._current_rel = ""
        self._done_count = 0
        self._total_count = 0
        self._active_files.clear()
        self._file_speed_samples.clear()
        self._slot_expanded = False
        try:
            self.slot_panel.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass
        self._phase = _PHASE_IDLE
        self._abort_flag = False
        self._resume_after_scan = False
        self._resume_strategies = {}
        self._resume_checked = {}
        self._baseline_strategies = {}
        self._overrides_fallback = False
        self._strategy_snapshot = {"checked": {}, "strategies": {}}
        self._cleanup_temp_dir()

        self._teardown_pending_ui()
        self._pending_mode = "wait"
        self._pending_errors.clear()
        self._pending_ready = False
        self._give_up = False
        self._give_up_missing = []
        self._record_marker = None
        self._record_cache_root = None
        self._record_rels = []

        self.strategy_table.set_folders([])
        self.change_list.load(None)
        self.drop_zip.set_step_hint(
            step_text="STEP 2 · 拖入更新包",
            subtitle="支持 .zip / .eapack")
        try:
            self.drop_zip.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass

        self._set_button_state(_BTN_DISABLED)
        try:
            self.progress_wrap.grid_remove()
            self.hint_wrap.grid_remove()
            self.progress.set_file_progress(None)
        except Exception:  # noqa: BLE001, S110
            pass
        self._refresh_changelog_button()

    def _on_clear_pack_click(self):
        if self._phase != _PHASE_IDLE:
            self.log.log("warn", "更新流程进行中，暂时不能清空")
            return
        if self.app_state.is_locked:
            return
        self._reset_pack_state()
        try:
            self.drop_zip.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass
        self.log.clear()
        if self.pack_info is not None:
            self.log.log("info",
                         f"整合包：{self.pack_info.name}"
                         f"（{self.pack_info.mod_count} 个模组）")
            self.log.log("info", "请拖入更新包（.zip / .eapack）")
            self._set_hint("拖入更新包后即可开始")
        self._refresh_ui_state()


# ----------------------------------------------------------------------
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


def _shorten_path(path: str, max_len: int = 60) -> str:
    if not path:
        return ""
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    if len(parts) >= 3:
        head = parts[0]
        tail = parts[-1]
        candidate = f"{head}/…/{tail}"
        if len(candidate) <= max_len:
            return candidate
        half = (max_len - 3) // 2
        return candidate[:half] + "…" + candidate[-half:]
    half = (max_len - 3) // 2
    return path[:half] + "…" + path[-half:]
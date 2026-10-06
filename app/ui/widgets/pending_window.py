"""
pending_window.py — 更新受阻 / 补齐缺口 子窗口
================================================
**高瘦竖排**布局（约 500×720），一屏一件事，不需要任何程序基础：

    ① 点蓝色的「源名称」用浏览器下载
    ② 把下载好的文件拖进本页的框里

设计要点：
  * 非模态：`transient` 但不 `grab_set` → 主窗口照常可用，也不会阻塞事件循环
  * **不阻塞线程**：本窗口只画界面；下载/校验/复制全部由调用方在后台线程做，
    状态通过 `report_*` 方法回主线程更新
  * 每页一个独立拖入框（懒创建 + 补齐即销毁，不会不停造控件）
  * 拖进来的文件若属于**别的页** → 自动切到那一页并完成补入
  * 关闭窗口只是**收起**（回调 `on_hide`），绝不等于放弃
  * `destroy()` 会取消所有挂起的 `after`，不留定时器

调用方需要提供：
    on_file_dropped(rel, path) -> bool      校验并收下这个文件
    on_retry(rel, report)                    重新下载（自己起后台线程）
    on_all_resolved()                        所有页都补齐了
    on_give_up()                             （wait 模式）放弃并完成更新
    on_dismiss()                             （complete 模式）不再提醒
"""

from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...core import pending as pending_logic
from ...theme import Color, Font, Size
from .drop_zone import DropZone
from .tooltip import attach_tooltip

_WIN_W, _WIN_H = 500, 720
_WIN_MIN_W, _WIN_MIN_H = 440, 620
_DROP_H = 150
_LIST_H = 96


class PendingWindow(ctk.CTkToplevel):
    def __init__(self, master, *,
                 mode: str = "wait",
                 items: list[dict] | None = None,
                 on_file_dropped: Callable[[str, Path], bool] | None = None,
                 on_retry: Callable[[str, Callable[[str, str], None]], None]
                 | None = None,
                 on_all_resolved: Callable[[], None] | None = None,
                 on_give_up: Callable[[], None] | None = None,
                 on_dismiss: Callable[[], None] | None = None,
                 on_hide: Callable[[], None] | None = None,
                 on_notice: Callable[[str, str], None] | None = None,
                 **kwargs):
        super().__init__(master, **kwargs)

        self.mode = mode
        self.on_file_dropped = on_file_dropped
        self.on_retry = on_retry
        self.on_all_resolved = on_all_resolved
        self.on_give_up = on_give_up
        self.on_dismiss = on_dismiss
        self.on_hide = on_hide
        self.on_notice = on_notice

        self._items: dict[str, dict] = {}
        self._order: list[str] = []
        self._index: int = 0
        self._drops: dict[str, DropZone] = {}
        self._retrying: set[str] = set()
        self._after_ids: list[str] = []
        self._closed = False

        self._build()

        for item in items or []:
            self.add_item(item)
        self._refresh()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self):
        try:
            self.title("更新受阻" if self.mode == "wait"
                       else "上次更新未完成")
            self.geometry(f"{_WIN_W}x{_WIN_H}")
            self.minsize(_WIN_MIN_W, _WIN_MIN_H)
            self.configure(fg_color=Color.WINDOW_BG)
            self.protocol("WM_DELETE_WINDOW", self.hide_window)
            try:
                self.transient(self.master)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001, S110
                pass
        except Exception:  # noqa: BLE001, S110
            pass

        self.grid_columnconfigure(0, weight=1)
        # 只有"拖入框"那一行吃剩余高度 → 整体高瘦、框永远够大
        self.grid_rowconfigure(5, weight=1)

        # 顶部警示色条（"任务受阻"的视觉基调）
        try:
            ctk.CTkFrame(self, fg_color=Color.WARNING, height=3,
                         corner_radius=0).grid(row=0, column=0, sticky="ew")
        except Exception:  # noqa: BLE001, S110
            pass

        head = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                            corner_radius=Size.RADIUS_CARD)
        head.grid(row=1, column=0, sticky="ew",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 8))
        head.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            head, text="更新受阻", font=Font.SUBTITLE,
            text_color=Color.WARNING, fg_color=Color.TRANSPARENT, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w",
                              padx=(12, 6), pady=(10, 0))

        self.badge = ctk.CTkLabel(
            head, text=" 任务受阻 ", font=Font.TINY,
            text_color=Color.WARNING, fg_color=Color.LOG_BG,
            corner_radius=6)
        self.badge.grid(row=0, column=1, sticky="e", padx=(0, 12),
                        pady=(10, 0))

        self.steps_label = ctk.CTkLabel(
            head, text=("① 点蓝色的「源名称」用浏览器下载\n"
                        "② 把下载好的文件拖进下面的框"),
            font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w", justify="left")
        self.steps_label.grid(row=1, column=0, columnspan=2, sticky="w",
                              padx=12, pady=(4, 10))

        # 目录条（滚轮切页 / 点击跳页）
        nav = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                           corner_radius=Size.RADIUS_CARD)
        nav.grid(row=2, column=0, sticky="ew",
                 padx=Size.PAD_PANEL, pady=(0, 8))
        nav.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(nav, fg_color=Color.TRANSPARENT)
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        bar.grid_columnconfigure(0, weight=1)

        self.count_label = ctk.CTkLabel(
            bar, text="待补入 (0)", font=Font.SMALL,
            text_color=Color.TEXT_PRIMARY, fg_color=Color.TRANSPARENT,
            anchor="w")
        self.count_label.grid(row=0, column=0, sticky="w")

        self.prev_btn = ctk.CTkButton(
            bar, text="◀", width=34, height=24, font=Font.SMALL,
            corner_radius=6, fg_color=Color.LOG_BG,
            hover_color=Color.BORDER, text_color=Color.TEXT_SECONDARY,
            command=lambda: self._step(-1))
        self.prev_btn.grid(row=0, column=1, sticky="e")

        self.page_label = ctk.CTkLabel(
            bar, text="0 / 0", font=Font.SMALL,
            text_color=Color.TEXT_SECONDARY, fg_color=Color.TRANSPARENT)
        self.page_label.grid(row=0, column=2, sticky="e", padx=6)

        self.next_btn = ctk.CTkButton(
            bar, text="▶", width=34, height=24, font=Font.SMALL,
            corner_radius=6, fg_color=Color.LOG_BG,
            hover_color=Color.BORDER, text_color=Color.TEXT_SECONDARY,
            command=lambda: self._step(1))
        self.next_btn.grid(row=0, column=3, sticky="e")

        self.list_frame = ctk.CTkScrollableFrame(
            nav, fg_color=Color.TRANSPARENT, corner_radius=0,
            height=_LIST_H,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.list_frame.grid(row=1, column=0, sticky="ew", padx=8,
                             pady=(4, 8))
        self.list_frame.grid_columnconfigure(0, weight=1)
        try:
            self.list_frame._scrollbar.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass
        # 滚轮在目录上 = 切页（简单动态高亮，不需要"聚光灯"引导）
        for widget in (self.list_frame, nav):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    widget.bind(seq, self._on_wheel,
                                add="+")  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001, S110
                    pass

        self._list_rows: dict[str, ctk.CTkButton] = {}

        # 当前页详情
        detail = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                              corner_radius=Size.RADIUS_CARD)
        detail.grid(row=3, column=0, sticky="ew",
                    padx=Size.PAD_PANEL, pady=(0, 8))
        detail.grid_columnconfigure(1, weight=1)

        self.name_label = ctk.CTkLabel(
            detail, text="", font=Font.BODY_B,
            text_color=Color.TEXT_PRIMARY, fg_color=Color.TRANSPARENT,
            anchor="w", justify="left")
        self.name_label.grid(row=0, column=0, columnspan=3, sticky="w",
                             padx=12, pady=(10, 2))
        attach_tooltip(self.name_label, self._current_rel_or_empty)

        self.sources_frame = ctk.CTkFrame(detail, fg_color=Color.TRANSPARENT)
        self.sources_frame.grid(row=1, column=0, columnspan=3, sticky="ew",
                                padx=12)
        self.sources_frame.grid_columnconfigure(1, weight=1)

        self.error_label = ctk.CTkLabel(
            detail, text="", font=Font.TINY,
            text_color=Color.TEXT_MUTED, fg_color=Color.TRANSPARENT,
            anchor="w", justify="left")
        self.error_label.grid(row=2, column=0, columnspan=3, sticky="w",
                              padx=12, pady=(2, 10))

        # 每页独立拖入框（懒创建）
        self.drops = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.drops.grid(row=5, column=0, sticky="nsew",
                        padx=Size.PAD_PANEL, pady=(0, 8))
        self.drops.grid_columnconfigure(0, weight=1)
        self.drops.grid_rowconfigure(0, weight=1)

        # 状态行 + 底部按钮
        self.status_label = ctk.CTkLabel(
            self, text="", font=Font.SMALL,
            text_color=Color.TEXT_SECONDARY, fg_color=Color.TRANSPARENT,
            anchor="w", justify="left")
        self.status_label.grid(row=4, column=0, sticky="ew",
                               padx=Size.PAD_PANEL)

        foot = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        foot.grid(row=6, column=0, sticky="ew",
                  padx=Size.PAD_PANEL, pady=(0, Size.PAD_PANEL))
        foot.grid_columnconfigure(1, weight=1)

        self.retry_btn = ctk.CTkButton(
            foot, text="重新下载", width=110, height=34, font=Font.BUTTON,
            corner_radius=Size.RADIUS_BUTTON, fg_color=Color.LOG_BG,
            hover_color=Color.BORDER, text_color=Color.TEXT_PRIMARY,
            command=self._on_retry_click)
        self.retry_btn.grid(row=0, column=0, sticky="w")

        if self.mode == "wait":
            self.alt_btn = ctk.CTkButton(
                foot, text="放弃并完成更新", width=150, height=34,
                font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.ERROR,
                command=self._fire(self.on_give_up))
        else:
            self.alt_btn = ctk.CTkButton(
                foot, text="不再提醒", width=110, height=34,
                font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.TEXT_SECONDARY,
                command=self._fire(self.on_dismiss))
        self.alt_btn.grid(row=0, column=2, sticky="e")

    @staticmethod
    def _fire(cb):
        def _inner():
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001, S110
                    pass
        return _inner

    # ==================================================================
    # 数据
    # ==================================================================
    def set_items(self, items: list[dict]):
        self._items.clear()
        self._order.clear()
        self._index = 0
        for item in items or []:
            self.add_item(item, refresh=False)
        self._refresh()

    def add_item(self, item: dict, refresh: bool = True):
        rel = str(item.get("rel", "") or "")
        if not rel or rel in self._items:
            if refresh:
                self._refresh()
            return
        self._items[rel] = dict(item)
        self._order.append(rel)
        if refresh:
            self._refresh()

    def pending_count(self) -> int:
        return len(self._order)

    def has_pending(self) -> bool:
        return bool(self._order)

    def pending_rels(self) -> list[str]:
        return list(self._order)

    def current_rel(self) -> str:
        if not self._order:
            return ""
        self._index = max(0, min(self._index, len(self._order) - 1))
        return self._order[self._index]

    def _current_rel_or_empty(self) -> str:
        rel = self.current_rel()
        return rel or ""

    def resolve(self, rel: str, notify: bool = True):
        """这一页的文件已经补齐 → 关闭该页。"""
        if rel not in self._items:
            return
        self._items.pop(rel, None)
        if rel in self._order:
            idx = self._order.index(rel)
            self._order.remove(rel)
            if idx < self._index:
                self._index -= 1
        drop = self._drops.pop(rel, None)
        if drop is not None:
            try:
                drop.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        self._retrying.discard(rel)
        self._index = min(self._index, max(0, len(self._order) - 1))
        self._refresh()
        if not self._order and notify:
            self._status("全部补齐，正在继续…")
            cb = self.on_all_resolved
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001, S110
                    pass

    # ==================================================================
    # 刷新
    # ==================================================================
    def _refresh(self):
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except Exception:  # noqa: BLE001, S110
                pass
        self._after_ids.clear()
        try:
            self._refresh_title()
            self._refresh_list()
            self._refresh_page()
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_title(self):
        n = len(self._order)
        if self.mode == "wait":
            text = f"更新受阻：还差 {n} 个文件" if n else "更新受阻"
        else:
            text = f"上次更新未完成：还差 {n} 个文件" if n else "上次更新未完成"
        try:
            self.title_label.configure(
                text=("⚠ " + text) if n else text)
            self.count_label.configure(text=f"待补入 ({n})")
            self.page_label.configure(
                text=f"{self._index + 1} / {n}" if n else "0 / 0")
            state = "normal" if n else "disabled"
            self.prev_btn.configure(state=state)
            self.next_btn.configure(state=state)
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_list(self):
        try:
            for w in self.list_frame.winfo_children():
                w.destroy()
        except Exception:  # noqa: BLE001, S110
            pass
        self._list_rows = {}
        cur = self.current_rel()
        for i, rel in enumerate(self._order):
            is_cur = (rel == cur)
            try:
                btn = ctk.CTkButton(
                    self.list_frame,
                    text=("● " if is_cur else "○ ") + Path(rel).name,
                    anchor="w", font=Font.SMALL, height=26,
                    corner_radius=6,
                    fg_color=(Color.LOG_BG if is_cur
                              else Color.TRANSPARENT),
                    hover_color=Color.BORDER,
                    text_color=(Color.WARNING if is_cur
                                else Color.TEXT_SECONDARY),
                    command=lambda r=rel: self._goto(r))
                btn.grid(row=i, column=0, sticky="ew", padx=2, pady=1)
                self._list_rows[rel] = btn
            except Exception:  # noqa: BLE001, S112
                continue

    def _refresh_page(self):
        rel = self.current_rel()
        item = self._items.get(rel, {})
        try:
            self.name_label.configure(
                text=Path(rel).name if rel else "没有待补入的文件")
            self.error_label.configure(
                text=("上次失败：" + str(item.get("error"))
                      if item.get("error") else "")
                if rel else "")
        except Exception:  # noqa: BLE001, S110
            pass

        self._refresh_sources(item)
        self._show_drop(rel)
        try:
            running = rel in self._retrying
            self.retry_btn.configure(
                state="disabled" if (not rel or running) else "normal",
                text="正在重新下载…" if running else "重新下载")
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_sources(self, item: dict):
        for w in self.sources_frame.winfo_children():
            try:
                w.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        rel = str(item.get("rel", "") or "")
        rows = pending_logic.source_rows(item.get("urls") or [])
        if not rows:
            ctk.CTkLabel(
                self.sources_frame, text="来源　　没有可用的下载链接",
                font=Font.SMALL, text_color=Color.TEXT_MUTED,
                fg_color=Color.TRANSPARENT, anchor="w")\
                .grid(row=0, column=0, columnspan=2, sticky="w")
            return
        for i, (name, url) in enumerate(rows):
            ctk.CTkLabel(
                self.sources_frame, text=("来源　　" if i == 0 else ""),
                font=Font.SMALL, text_color=Color.TEXT_MUTED,
                fg_color=Color.TRANSPARENT, anchor="w")\
                .grid(row=i, column=0, sticky="w")
            link = ctk.CTkButton(
                self.sources_frame, text=name, font=Font.SMALL,
                height=24, width=1, anchor="w", corner_radius=6,
                fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
                text_color=Color.ACCENT,
                command=lambda u=url: self._open_url(u))
            link.grid(row=i, column=1, sticky="w", padx=(0, 4))
            attach_tooltip(link, url)
            copy_btn = ctk.CTkButton(
                self.sources_frame, text="复制", font=Font.TINY,
                width=44, height=22, corner_radius=6,
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.TEXT_SECONDARY,
                command=lambda u=url, r=rel: self._copy_url(u, r))
            copy_btn.grid(row=i, column=2, sticky="e")

    def _show_drop(self, rel: str):
        """只显示当前页的拖入框（每页一个，懒创建，补齐即销毁）。"""
        for r, drop in list(self._drops.items()):
            try:
                if r == rel:
                    drop.grid()
                else:
                    drop.grid_remove()
            except Exception:  # noqa: BLE001, S112
                continue
        if not rel:
            return
        drop = self._drops.get(rel)
        if drop is None:
            try:
                drop = DropZone(
                    self.drops,
                    title=f"把 {Path(rel).name} 拖到这里补入",
                    subtitle="拖入后自动校验，通过即关闭本页",
                    height=_DROP_H, mode="any",
                    on_drop=lambda paths, r=rel: self._handle_drop(r, paths),
                    highlight=True)
                drop.grid(row=0, column=0, sticky="nsew")
                self._drops[rel] = drop
            except Exception:  # noqa: BLE001
                return
        else:
            try:
                drop.set_text(
                    title=f"把 {Path(rel).name} 拖到这里补入",
                    subtitle="拖入后自动校验，通过即关闭本页")
                drop.grid()
            except Exception:  # noqa: BLE001, S110
                pass

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_wheel(self, event):
        try:
            delta = getattr(event, "delta", 0) or 0
            num = getattr(event, "num", 0) or 0
            if num == 4 or delta > 0:
                self._step(-1)
            elif num == 5 or delta < 0:
                self._step(1)
        except Exception:  # noqa: BLE001, S110
            pass

    def _step(self, delta: int):
        if not self._order:
            return
        self._index = (self._index + delta) % len(self._order)
        self._refresh()

    def _goto(self, rel: str):
        if rel in self._order:
            self._index = self._order.index(rel)
            self._refresh()

    def _show_first_unresolved(self):
        if self._order:
            self._index = 0
        self._refresh()

    def _handle_drop(self, current_rel: str, paths: list[Path]):
        for p in paths or []:
            hits = pending_logic.match_candidates(p.name, self._order)
            target = current_rel if current_rel in hits else (
                hits[0] if hits else "")
            if not target:
                self._notice("warn", f"{p.name} 不在待补入列表里，已忽略")
                continue
            if target != current_rel:
                self._goto(target)
                self._notice(
                    "info",
                    f"这个文件是「{Path(target).name}」需要的，已切换到那一页")
            ok = False
            if self.on_file_dropped is not None:
                try:
                    ok = bool(self.on_file_dropped(target, p))
                except Exception:  # noqa: BLE001
                    ok = False
            if ok:
                self._notice("info", f"{Path(target).name} 校验通过")
                self.resolve(target)
            else:
                self._notice("error", f"{p.name} 校验未通过，没有补入")
                self._status("校验未通过：文件可能不完整或不是这个版本")

    def _open_url(self, url: str):
        try:
            import webbrowser
            webbrowser.open(url)
            self._status("已在浏览器中打开下载页；下好后拖进上面的框")
        except Exception as e:  # noqa: BLE001
            self._notice("error", f"打开链接失败：{e}")

    def _copy_url(self, url: str, rel: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(pending_logic.links_text(rel, [url]))
            self._status("已复制下载链接")
        except Exception as e:  # noqa: BLE001
            self._notice("error", f"复制失败：{e}")

    def _on_retry_click(self):
        rel = self.current_rel()
        if not rel or rel in self._retrying or self.on_retry is None:
            return
        self._retrying.add(rel)
        self.report_retry(rel, "running", "")
        try:
            self.on_retry(rel, self._reporter(rel))
        except Exception as e:  # noqa: BLE001
            self._retrying.discard(rel)
            self.report_retry(rel, "failed", str(e))

    def _reporter(self, rel: str):
        def _report(status: str, message: str = ""):
            self._later(lambda: self.report_retry(rel, status, message))
        return _report

    def report_retry(self, rel: str, status: str, message: str = ""):
        """由调用方在**主线程**回调：running / ok / failed。"""
        if status == "running":
            self._retrying.add(rel)
            self._status("正在重新下载…")
        else:
            self._retrying.discard(rel)
            if status == "ok":
                self._status("重新下载成功")
                self.resolve(rel)
                return
            self._status(f"重新下载失败：{message or '仍然连不上'}")
            item = self._items.get(rel)
            if item is not None and message:
                item["error"] = message
        if rel == self.current_rel():
            self._refresh_page()

    # ==================================================================
    # 生命周期 / 工具
    # ==================================================================
    def _later(self, fn, delay_ms: int = 0):
        if self._closed:
            return
        try:
            after_id = self.after(delay_ms, fn)
            if isinstance(after_id, str):
                self._after_ids.append(after_id)
        except Exception:  # noqa: BLE001, S110
            pass

    def _status(self, text: str):
        try:
            self.status_label.configure(text=text)
        except Exception:  # noqa: BLE001, S110
            pass

    def _notice(self, level: str, text: str):
        self._status(text)
        if self.on_notice is not None:
            try:
                self.on_notice(level, text)
            except Exception:  # noqa: BLE001, S110
                pass

    def hide_window(self):
        """收起（不是放弃）：主窗口那边会留一个"打开补入窗口"的入口。"""
        try:
            self.withdraw()
        except Exception:  # noqa: BLE001, S110
            pass
        if self.on_hide is not None:
            try:
                self.on_hide()
            except Exception:  # noqa: BLE001, S110
                pass

    def show_window(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001, S110
            pass
        self._show_first_unresolved()

    def destroy(self):
        self._closed = True
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except Exception:  # noqa: BLE001, S110
                pass
        self._after_ids.clear()
        self._retrying.clear()
        self._drops.clear()
        try:
            super().destroy()
        except Exception:  # noqa: BLE001, S110
            pass

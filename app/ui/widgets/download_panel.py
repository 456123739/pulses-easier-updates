"""
download_panel.py — 下载失败断点投入面板（支持实时追加）
------------------------------------------------
- 支持下载进行中实时追加失败项（add_item）
- 支持空列表初始化
- 列表 + 拖入补入框
- 每一行给出「文件名 + 失败原因 + 下载链接」，并提供
  「复制链接」「打开链接」两个按钮 —— 否则链接只是一行不可选中的文字，
  用户根本抄不走，"甩给用户让他自己去下"就落空了。
- 拖入时按文件名匹配（容忍浏览器加的 "(1)"/"- 副本" 后缀、大小写差异），
  匹配不上或校验不过都会通过 on_notice 明确告知，不再静默失败。
"""

import re
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...theme import Color, Font, Size
from .drop_zone import DropZone

# 浏览器/系统给重复下载加的后缀："xxx (1).jar"、"xxx - 副本.jar"、"xxx - copy.jar"
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*\(\d+\)|\s*-\s*副本|\s*-\s*copy|\s*_副本)+$", re.IGNORECASE)


class DownloadPanel(ctk.CTkFrame):
    """
    参数：
      failed_items: list[dict]    初始失败项（可为空）
      on_file_dropped(rel_path, file_path) -> bool
      on_skip_all() -> None
      in_progress: bool           是否下载仍在进行中
      on_notice(level, text)      可选的日志/提示回调
    """

    def __init__(self, master,
                 failed_items: list[dict],
                 on_file_dropped: Callable[[str, Path], bool],
                 on_skip_all: Callable[[], None],
                 in_progress: bool = False,
                 on_notice: Callable[[str, str], None] | None = None,
                 on_all_resolved: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self._failed: dict[str, dict] = {it["rel"]: it for it in failed_items}
        self.on_file_dropped = on_file_dropped
        self.on_skip_all = on_skip_all
        self.on_notice = on_notice
        # 待补入列表被清空时回调：让上层能继续走"应用更新"，
        # 而不是把面板一藏就再也没有出口
        self.on_all_resolved = on_all_resolved
        self._in_progress = in_progress
        self._row_index = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 标题 + 跳过按钮
        head = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        head.grid(row=0, column=0, sticky="ew",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 6))
        head.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            head, text="", font=Font.SUBTITLE,
            text_color=Color.WARNING,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(head, text="全部跳过", width=90, height=28,
                      font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self.on_skip_all)\
            .grid(row=0, column=1, sticky="e")

        # 列表
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=Color.TRANSPARENT, corner_radius=0,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.scroll.grid(row=1, column=0, sticky="nsew",
                         padx=8, pady=(0, 6))
        self.scroll.grid_columnconfigure(0, weight=1)
        try:
            self.scroll._scrollbar.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass

        self.empty_label = ctk.CTkLabel(
            self.scroll, text="暂无失败项",
            font=Font.SMALL, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT)
        self.empty_label.grid(row=0, column=0, sticky="w",
                              padx=4, pady=6)

        # 初始项
        for item in failed_items:
            self._append_row(item)

        # 投入框
        self.drop = DropZone(
            self,
            title="把文件拖到这里补入",
            subtitle="将重新校验",
            height=Size.DROP_H_WORK - 20,
            mode="any",
            on_drop=self._on_files_dropped)
        self.drop.grid(row=2, column=0, sticky="ew",
                       padx=Size.PAD_PANEL, pady=(0, Size.PAD_PANEL))

        self._refresh_title()
        self._refresh_empty()

    # ------------------------------------------------------------------
    def add_item(self, item: dict):
        """实时追加失败项"""
        rel = item.get("rel", "")
        if not rel or rel in self._failed:
            return
        self._failed[rel] = item
        self._append_row(item)
        self._refresh_title()
        self._refresh_empty()

    def set_in_progress(self, in_progress: bool):
        self._in_progress = in_progress
        self._refresh_title()

    def has_failures(self) -> bool:
        return bool(self._failed)

    # ------------------------------------------------------------------
    def _refresh_title(self):
        n = len(self._failed)
        if self._in_progress:
            text = f"下载中 · 已失败 {n} 项"
        else:
            text = f"下载失败（{n} 项）"
        try:
            self.title_label.configure(text=text)
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_empty(self):
        try:
            if self._failed:
                self.empty_label.grid_remove()
            else:
                self.empty_label.grid(row=0, column=0, sticky="w",
                                      padx=4, pady=6)
        except Exception:  # noqa: BLE001, S110
            pass

    def _append_row(self, item: dict):
        row = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT)
        row.grid(row=self._row_index + 1, column=0, sticky="ew", pady=2)
        row.grid_columnconfigure(0, weight=1)
        self._row_index += 1
        rel = item["rel"]

        ctk.CTkLabel(row, text=rel, font=Font.SMALL,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, columnspan=3, sticky="ew", padx=(4, 4))

        err = item.get("error", "")
        if err:
            ctk.CTkLabel(row, text=err, font=Font.TINY,
                         text_color=Color.ERROR,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .grid(row=1, column=0, columnspan=3, sticky="ew", padx=(4, 4))

        urls = self.links_of(rel)
        if urls:
            shown = urls[0]
            if len(urls) > 1:
                shown += f"   （共 {len(urls)} 个源，复制会带上全部）"
            ctk.CTkLabel(row, text=shown, font=Font.TINY,
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT, anchor="w",
                         wraplength=420, justify="left")\
                .grid(row=2, column=0, columnspan=3, sticky="ew",
                      padx=(4, 4))
            ctk.CTkButton(row, text="复制链接", width=68, height=22,
                          font=Font.TINY, corner_radius=6,
                          fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                          text_color=Color.TEXT_SECONDARY,
                          command=lambda r=rel: self._copy_links(r))\
                .grid(row=3, column=0, sticky="w", padx=(4, 4), pady=(2, 0))
            ctk.CTkButton(row, text="打开链接", width=68, height=22,
                          font=Font.TINY, corner_radius=6,
                          fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                          text_color=Color.TEXT_SECONDARY,
                          command=lambda r=rel: self._open_link(r))\
                .grid(row=3, column=1, sticky="w", padx=(0, 4), pady=(2, 0))

    # ------------------------------------------------------------------
    # 链接 / 匹配（纯逻辑，便于无界面测试）
    # ------------------------------------------------------------------
    def links_of(self, rel: str) -> list[str]:
        """该失败项的全部下载源（按给定顺序去重）。"""
        item = self._failed.get(rel) or {}
        out: list[str] = []
        for u in item.get("urls") or []:
            if isinstance(u, str) and u and u not in out:
                out.append(u)
        return out

    def links_text(self, rel: str) -> str:
        """复制到剪贴板的内容：一行一个源，首行带上文件名方便用户对照。"""
        urls = self.links_of(rel)
        if not urls:
            return ""
        name = Path(rel).name
        return "\n".join([f"# {name}"] + urls)

    @staticmethod
    def _normalize_name(name: str) -> str:
        """
        归一化文件名：去浏览器副本后缀 + 统一大小写。

        注意不能把版本号当后缀削掉 —— `create-1.20.1-6.0.7.jar` 必须原样保留，
        所以这里只认 "(1)" / "- 副本" / "- copy" 这几种**明确**的副本标记。
        """
        base = Path(str(name)).name.strip()
        stem, dot, suffix = base.rpartition(".")
        if not dot:                     # 没有扩展名
            stem, suffix = base, ""
        stem = _COPY_SUFFIX_RE.sub("", stem).strip()
        return (stem + ("." + suffix if suffix else "")).casefold()

    def _candidates(self, file_name: str) -> list[str]:
        """按拖进来的文件名，找出可能对应的待补入项（可能多个同名的）。"""
        want = self._normalize_name(file_name)
        hits = [rel for rel in self._failed
                if self._normalize_name(Path(rel).name) == want]
        if hits:
            return hits
        # 兜底：只剩一个待补入项时直接归给它（投进去的东西还会过 _verify，
        # 校验不过会被拒，所以这里放宽是安全的）
        if len(self._failed) == 1:
            return list(self._failed)
        return []

    def _notice(self, level: str, text: str):
        if self.on_notice is None:
            return
        try:
            self.on_notice(level, text)
        except Exception:  # noqa: BLE001, S110
            pass

    def _copy_links(self, rel: str):
        text = self.links_text(rel)
        if not text:
            self._notice("warn", f"{Path(rel).name} 没有可复制的下载链接")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._notice("info", f"已复制 {Path(rel).name} 的下载链接")
        except Exception as e:  # noqa: BLE001
            self._notice("error", f"复制失败：{e}")

    def _open_link(self, rel: str):
        urls = self.links_of(rel)
        if not urls:
            self._notice("warn", f"{Path(rel).name} 没有可打开的链接")
            return
        try:
            import webbrowser
            webbrowser.open(urls[0])
            self._notice("info", f"已在浏览器中打开 {Path(rel).name} 的下载页")
        except Exception as e:  # noqa: BLE001
            self._notice("error", f"打开链接失败：{e}")

    # ------------------------------------------------------------------
    def _on_files_dropped(self, paths: list[Path]):
        for p in paths:
            cands = self._candidates(p.name)
            if not cands:
                self._notice("warn",
                             f"{p.name} 不在待补入列表里，已忽略")
                continue
            resolved = False
            for rel in cands:
                ok = False
                try:
                    ok = bool(self.on_file_dropped(rel, p))
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    self._failed.pop(rel, None)
                    resolved = True
                    break
            if not resolved:
                self._notice("error",
                             f"{p.name} 校验未通过，没有补入")

        all_done = not self._failed
        if all_done:
            try:
                self.grid_remove()
            except Exception:  # noqa: BLE001, S110
                pass
        self._refresh_title()
        self._refresh_empty()

        # 回调放最后：上层可能在回调里把这个面板销毁
        if all_done and self.on_all_resolved is not None:
            try:
                self.on_all_resolved()
            except Exception:  # noqa: BLE001, S110
                pass
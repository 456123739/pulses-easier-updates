"""
change_list.py — 变更列表（折叠式，支持策略灰显）
------------------------------------------------
- 顶部概要行：+新增 ~修改 -删除
- 按顶层文件夹分组
- 文件夹级不可展开；文件级可展开
- 支持传入 checked / strategies：将被跳过的条目灰显 + 标注「已跳过」
"""

import unicodedata
from collections import defaultdict

import customtkinter as ctk

from ...config import ChangeKind
from ...core.apply_rules import will_skip as _rules_will_skip
from ...core.differ import Change, DiffResult
from ...theme import Color, Font, Size


# ----------------------------------------------------------------------
# 路径省略
# ----------------------------------------------------------------------
def _display_width(text: str) -> int:
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return w


def _hard_cut(s: str, max_width: int) -> str:
    if _display_width(s) <= max_width:
        return s
    half = max(1, (max_width - 3) // 2)

    def cut(text: str, w: int, from_end: bool) -> str:
        acc = 0
        out: list[str] = []
        chars = list(reversed(text)) if from_end else list(text)
        for ch in chars:
            cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
            if acc + cw > w:
                break
            out.append(ch)
            acc += cw
        return "".join(reversed(out)) if from_end else "".join(out)

    return cut(s, half, False) + "..." + cut(s, half, True)


def _shorten_path(path: str, max_width: int = 70) -> str:
    if _display_width(path) <= max_width:
        return path
    parts = path.split("/")
    if len(parts) >= 3:
        head = parts[0]
        tail = parts[-1]
        candidate = f"{head}/.../{tail}"
        return _hard_cut(candidate, max_width)
    return _hard_cut(path, max_width)


_KIND_COLOR = {
    ChangeKind.ADDED:    Color.ADDED,
    ChangeKind.MODIFIED: Color.MODIFIED,
    ChangeKind.DELETED:  Color.DELETED,
}
_KIND_MARKER = {
    ChangeKind.ADDED:    "+",
    ChangeKind.MODIFIED: "~",
    ChangeKind.DELETED:  "-",
}


# ----------------------------------------------------------------------
# 策略判定：该条目是否会被跳过（统一走 apply_rules，避免三套实现漂移）
# ----------------------------------------------------------------------
def _will_skip(change: Change, checked: dict, strategies: dict) -> bool:
    return _rules_will_skip(change, checked, strategies)


# ----------------------------------------------------------------------
class ChangeList(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self._diff: DiffResult | None = None
        self._checked: dict[str, bool] = {}
        self._strategies: dict[str, str] = {}
        self._expanded: set[str] = set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.grid(row=0, column=0, sticky="ew",
                    padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="变更内容", font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, sticky="w")

        self.summary = ctk.CTkFrame(header, fg_color=Color.TRANSPARENT)
        self.summary.grid(row=0, column=1, sticky="e")

        self.summary_added = ctk.CTkLabel(
            self.summary, text="", font=Font.SMALL,
            text_color=Color.ADDED, fg_color=Color.TRANSPARENT)
        self.summary_added.pack(side="left", padx=3)
        self.summary_modified = ctk.CTkLabel(
            self.summary, text="", font=Font.SMALL,
            text_color=Color.MODIFIED, fg_color=Color.TRANSPARENT)
        self.summary_modified.pack(side="left", padx=3)
        self.summary_deleted = ctk.CTkLabel(
            self.summary, text="", font=Font.SMALL,
            text_color=Color.DELETED, fg_color=Color.TRANSPARENT)
        self.summary_deleted.pack(side="left", padx=3)

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=Color.TRANSPARENT, corner_radius=0,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.scroll.grid(row=1, column=0, sticky="nsew",
                         padx=8, pady=(0, 8))
        self.scroll.grid_columnconfigure(0, weight=1)
        try:
            self.scroll._scrollbar.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def load(self, diff: DiffResult | None,
             checked: dict | None = None,
             strategies: dict | None = None):
        """
        载入变更。
        checked/strategies 传入时，按当前策略标记「已跳过」条目（灰显）。
        """
        self._diff = diff
        self._checked = dict(checked) if checked else {}
        self._strategies = dict(strategies) if strategies else {}
        self._expanded = set()
        self._rebuild()

    def update_marks(self, checked: dict | None = None,
                     strategies: dict | None = None):
        """策略/勾选改变时，不重载 diff，只重绘标记"""
        if checked is not None:
            self._checked = dict(checked)
        if strategies is not None:
            self._strategies = dict(strategies)
        self._rebuild()

    def clear(self):
        self.load(None)

    # ------------------------------------------------------------------
    def _rebuild(self):
        for w in self.scroll.winfo_children():
            w.destroy()

        diff = self._diff
        if diff is None:
            self.summary_added.configure(text="")
            self.summary_modified.configure(text="")
            self.summary_deleted.configure(text="")
            return

        n_add = len(diff.added)
        n_mod = len(diff.modified)
        n_del = len(diff.deleted)
        self.summary_added.configure(text=f"+{n_add}")
        self.summary_modified.configure(text=f"~{n_mod}")
        self.summary_deleted.configure(text=f"-{n_del}")

        if n_add + n_mod + n_del == 0:
            return

        groups: dict[str, list[Change]] = defaultdict(list)
        for ch in diff.added:
            groups[ch.rel_path.parts[0] if ch.rel_path.parts else ""].append(ch)
        for ch in diff.modified:
            groups[ch.rel_path.parts[0] if ch.rel_path.parts else ""].append(ch)
        for ch in diff.deleted:
            groups[ch.rel_path.parts[0] if ch.rel_path.parts else ""].append(ch)

        row = 0
        for top in sorted(groups):
            changes = groups[top]
            a = sum(1 for c in changes if c.kind == ChangeKind.ADDED)
            m = sum(1 for c in changes if c.kind == ChangeKind.MODIFIED)
            d = sum(1 for c in changes if c.kind == ChangeKind.DELETED)

            has_file_level = any(not c.is_folder_level for c in changes)
            is_expandable = has_file_level
            expanded = top in self._expanded

            # 组级：统计有多少条会被跳过
            skip_count = sum(1 for c in changes
                             if self._marks_ready()
                             and _will_skip(c, self._checked,
                                            self._strategies))

            self._add_group_row(row, top, a, m, d,
                                is_expandable, expanded, skip_count)
            row += 1

            if expanded and is_expandable:
                for ch in changes:
                    self._add_sub_row(row, ch)
                    row += 1

    def _marks_ready(self) -> bool:
        return bool(self._checked) or bool(self._strategies)

    # ------------------------------------------------------------------
    def _add_group_row(self, row: int, top: str,
                       a: int, m: int, d: int,
                       expandable: bool, expanded: bool,
                       skip_count: int = 0):
        item = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT,
                            height=22)
        item.grid(row=row, column=0, sticky="ew", pady=0)
        item.grid_propagate(False)
        item.grid_columnconfigure(1, weight=1)

        arrow_text = ""
        if expandable:
            arrow_text = "▼" if expanded else "▶"
        arrow = ctk.CTkLabel(item, text=arrow_text,
                             font=(Font.FAMILY, 10),
                             text_color=Color.TEXT_MUTED,
                             fg_color=Color.TRANSPARENT,
                             width=12, anchor="w")
        arrow.grid(row=0, column=0, sticky="w", padx=(2, 0))

        if "/" in top or top == "":
            display_name = top
        else:
            display_name = f"{top}/"
        label = ctk.CTkLabel(item, text=f"📁 {display_name}",
                             font=(Font.FAMILY, 12, "bold"),
                             text_color=Color.TEXT_PRIMARY,
                             fg_color=Color.TRANSPARENT, anchor="w")
        label.grid(row=0, column=1, sticky="w", padx=(2, 0))

        summary = ctk.CTkFrame(item, fg_color=Color.TRANSPARENT)
        summary.grid(row=0, column=2, sticky="e", padx=(0, 4))
        if a:
            ctk.CTkLabel(summary, text=f"+{a}",
                         font=(Font.FAMILY, 11),
                         text_color=Color.ADDED,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left", padx=2)
        if m:
            ctk.CTkLabel(summary, text=f"~{m}",
                         font=(Font.FAMILY, 11),
                         text_color=Color.MODIFIED,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left", padx=2)
        if d:
            ctk.CTkLabel(summary, text=f"-{d}",
                         font=(Font.FAMILY, 11),
                         text_color=Color.DELETED,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left", padx=2)
        if skip_count:
            ctk.CTkLabel(summary, text=f"跳过 {skip_count}",
                         font=(Font.FAMILY, 10),
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left", padx=(6, 2))

        if expandable:
            for w in (item, label, arrow, summary):
                w.bind("<Button-1>",
                       lambda e, t=top: self._toggle_group(t))

    def _add_sub_row(self, row: int, change: Change):
        rel = change.rel_path
        if len(rel.parts) > 1:
            display = "/".join(rel.parts[1:])
        else:
            display = rel.name
        if change.is_folder_level:
            display += "/"

        marker = _KIND_MARKER[change.kind]
        base_color = _KIND_COLOR[change.kind]

        skipped = False
        if self._marks_ready():
            skipped = _will_skip(change, self._checked, self._strategies)

        color = Color.TEXT_MUTED if skipped else base_color

        item = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT,
                            height=20)
        item.grid(row=row, column=0, sticky="ew", pady=0)
        item.grid_propagate(False)
        item.grid_columnconfigure(1, weight=1)
        item.grid_columnconfigure(2, weight=0)

        ctk.CTkLabel(item, text=marker,
                     font=(Font.FAMILY, 11, "bold"),
                     text_color=color, fg_color=Color.TRANSPARENT,
                     width=12, anchor="w")\
            .grid(row=0, column=0, sticky="w", padx=(18, 0))

        ctk.CTkLabel(item, text=_shorten_path(display),
                     font=(Font.FAMILY, 11), text_color=color,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=1, sticky="w", padx=(2, 4))

        if skipped:
            ctk.CTkLabel(item, text="已跳过",
                         font=(Font.FAMILY, 10),
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT, anchor="e")\
                .grid(row=0, column=2, sticky="e", padx=(0, 6))

    def _toggle_group(self, top: str):
        if top in self._expanded:
            self._expanded.discard(top)
        else:
            self._expanded.add(top)
        self._rebuild()
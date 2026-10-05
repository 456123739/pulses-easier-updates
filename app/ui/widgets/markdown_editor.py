"""
markdown_editor.py — Markdown 更新日志编辑器
------------------------------------------------
- 编辑区：CTkTextbox
- 嵌入式预览：CTkTextbox + tk.Text tag
- 大屏预览：独立进程 + pywebview
- 编辑/预览切换：缓入缓出曲线动画
- 弹窗使用自定义组件
"""

import tkinter.font as tkfont
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.markdown_renderer import (
    html_to_tk,
    is_engine_available,
    open_preview_window,
    plain_text_html,
    render_markdown,
    tk_plain_text,
    wrap_html,
)
from ...theme import Color, Font, Size
from .dialog import alert

_MONO_FONT_CANDIDATES = [
    "Sarasa Mono SC", "Sarasa Term SC", "Cascadia Mono",
    "Consolas", "Courier New", "DejaVu Sans Mono",
]
_mono_font_name: str | None = None


def _pick_mono_font() -> str:
    global _mono_font_name
    if _mono_font_name is not None:
        return _mono_font_name
    try:
        available = set(tkfont.families())
    except Exception:  # noqa: BLE001
        available = set()
    for name in _MONO_FONT_CANDIDATES:
        if name in available:
            _mono_font_name = name
            return name
    _mono_font_name = "Consolas"
    return _mono_font_name


def _ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    return 1 - (-2 * t + 2) ** 3 / 2


def _configure_render_tags(tb, spacing: int, base_size: int = 13):
    sp = spacing
    fam = Font.FAMILY
    mono = _pick_mono_font()

    tb.tag_configure("h1", font=(fam, base_size + 8, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("h2", font=(fam, base_size + 5, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("h3", font=(fam, base_size + 3, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("h4", font=(fam, base_size + 1, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("h5", font=(fam, base_size, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("h6", font=(fam, base_size, "bold"),
                     foreground=Color.TEXT_SECONDARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)

    tb.tag_configure("bold", font=(fam, base_size, "bold"),
                     foreground=Color.TEXT_PRIMARY)
    tb.tag_configure("italic", font=(fam, base_size, "italic"))
    tb.tag_configure("strike", overstrike=True,
                     foreground=Color.TEXT_SECONDARY)
    tb.tag_configure("code", font=(mono, base_size - 1),
                     foreground=Color.MODIFIED,
                     background=Color.LOG_BG)

    tb.tag_configure("p", spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("li", lmargin1=20, lmargin2=36,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("li_marker", foreground=Color.ACCENT)

    tb.tag_configure("quote", lmargin1=12, lmargin2=12,
                     foreground=Color.TEXT_SECONDARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("quote_mark", foreground=Color.ACCENT)

    tb.tag_configure("pre", font=(mono, base_size - 1),
                     foreground=Color.ADDED,
                     background=Color.LOG_BG,
                     lmargin1=12, lmargin2=12,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("hr", foreground=Color.BORDER,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("img", foreground=Color.TEXT_MUTED)
    tb.tag_configure("link_url", foreground=Color.TEXT_MUTED)

    tb.tag_configure("table_header", font=(mono, base_size, "bold"),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("table_cell", font=(mono, base_size),
                     foreground=Color.TEXT_PRIMARY,
                     spacing1=sp, spacing2=sp, spacing3=sp)
    tb.tag_configure("table_sep", font=(mono, base_size),
                     foreground=Color.BORDER)
    tb.tag_configure("table_rule", font=(mono, base_size),
                     foreground=Color.BORDER,
                     spacing1=sp, spacing2=sp, spacing3=sp)


_TOOLBAR_BASIC = [
    ("H1", "一级标题", "h1"),
    ("H2", "二级标题", "h2"),
    ("H3", "三级标题", "h3"),
    ("B", "加粗", "bold"),
    ("I", "斜体", "italic"),
    ("S", "删除线", "strike"),
    ("•", "无序列表", "ul"),
    ("1.", "有序列表", "ol"),
    ("“", "引用", "quote"),
    ("</>", "行内代码", "code"),
    ("🔗", "链接", "link"),
]

_TOOLBAR_ADVANCED = [
    ("⛶", "代码块", "codeblock"),
    ("―", "分隔线", "hr"),
    ("▦", "表格", "table"),
    ("🖼", "图片", "image"),
    ("↶", "撤销", "undo"),
    ("↷", "重做", "redo"),
]


class MarkdownEditor(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.grid_propagate(False)

        self._mode = "edit"
        self._render_after_id: str | None = None
        self._render_token = 0
        self._advanced_shown = False
        self._line_spacing = 0
        self._preview_tags_ready = False

        self._anim_after_id: str | None = None
        self._anim_progress = 0.0
        self._anim_start = 0.0
        self._anim_target = 0.0
        self._anim_step = 0
        self._anim_steps = 1

        self._render_suspended = False

        self._tip: ctk.CTkToplevel | None = None
        self._tip_visible = False
        self._tip_after_id: str | None = None
        self._tip_widget = None
        self._tip_text = ""

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_toolbar()
        self._build_main_area()
        self._build_footer()

        self.editor._textbox.bind("<<Modified>>", self._on_modified)
        self.editor._textbox.edit_modified(False)

        self.winfo_toplevel().bind("<Configure>", self._on_parent_configure,
                                   add="+")
        self.winfo_toplevel().bind("<Destroy>", self._on_parent_destroy,
                                   add="+")

    # ------------------------------------------------------------------
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        bar.grid(row=0, column=0, sticky="w",
                 padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 6))

        ctk.CTkLabel(bar, text="更新日志", font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, sticky="w", padx=(0, 16))

        ctk.CTkLabel(bar, text="行距", font=Font.TINY,
                     text_color=Color.TEXT_SECONDARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=1, sticky="w", padx=(0, 4))

        self.spacing_slider = ctk.CTkSlider(
            bar, from_=-6, to=20, number_of_steps=26,
            width=130, height=16,
            fg_color=Color.BORDER,
            progress_color=Color.ACCENT,
            button_color=Color.ACCENT,
            button_hover_color=Color.ACCENT_HOVER,
            command=self._on_spacing_change)
        self.spacing_slider.set(self._line_spacing)
        self.spacing_slider.grid(row=0, column=2, sticky="w", padx=(0, 12))

        self.full_btn = ctk.CTkButton(
            bar, text="⛶", width=28, height=28,
            font=Font.BODY, corner_radius=6,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._open_full_preview)
        self.full_btn.grid(row=0, column=3, sticky="w", padx=(0, 6))
        self._bind_tip(self.full_btn, "大屏预览（GitHub 风格）")

        self.mode_switch = ctk.CTkSegmentedButton(
            bar, values=["编辑", "预览"],
            font=Font.SMALL, height=28, width=140,
            fg_color=Color.LOG_BG,
            selected_color=Color.ACCENT,
            selected_hover_color=Color.ACCENT_HOVER,
            unselected_color=Color.LOG_BG,
            unselected_hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_mode_change)
        self.mode_switch.set("编辑")
        self.mode_switch.grid(row=0, column=4, sticky="w")

    def _on_spacing_change(self, value: float):
        self._line_spacing = round(value)
        if self._mode == "preview":
            self._schedule_render(immediate=True)

    # ------------------------------------------------------------------
    def _build_toolbar(self):
        self.toolbar = ctk.CTkFrame(self, fg_color=Color.LOG_BG,
                                    corner_radius=8)
        self.toolbar.grid(row=1, column=0, sticky="w",
                          padx=Size.PAD_PANEL, pady=(0, 6))

        basic = ctk.CTkFrame(self.toolbar, fg_color=Color.TRANSPARENT)
        basic.grid(row=0, column=0, sticky="w", padx=6, pady=(6, 3))
        for text, tip, key in _TOOLBAR_BASIC:
            self._make_tool_btn(basic, text, tip, key)

        self.more_btn = ctk.CTkButton(
            basic, text="更多", width=30, height=24,
            font=Font.TINY, corner_radius=6,
            fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._toggle_advanced)
        self.more_btn.pack(side="left", padx=(6, 0))
        self._bind_tip(self.more_btn, "展开更多格式")

        self.advanced = ctk.CTkFrame(self.toolbar,
                                     fg_color=Color.TRANSPARENT)
        self.advanced.grid(row=1, column=0, sticky="w",
                           padx=6, pady=(0, 6))
        for text, tip, key in _TOOLBAR_ADVANCED:
            self._make_tool_btn(self.advanced, text, tip, key)
        self.advanced.grid_remove()

    def _make_tool_btn(self, parent, text: str, tooltip: str, key: str):
        btn = ctk.CTkButton(
            parent, text=text, width=30, height=24,
            font=Font.SMALL, corner_radius=6,
            fg_color=Color.CARD_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=lambda k=key: self._apply_format(k))
        btn.pack(side="left", padx=1)
        self._bind_tip(btn, tooltip)
        return btn

    def _toggle_advanced(self):
        self._advanced_shown = not self._advanced_shown
        if self._advanced_shown:
            self.advanced.grid()
            self.more_btn.configure(text="更多")
        else:
            self.advanced.grid_remove()
            self.more_btn.configure(text="更多")

    # ------------------------------------------------------------------
    # Tooltip
    # ------------------------------------------------------------------
    def _bind_tip(self, widget, text: str):
        widget.bind("<Enter>", lambda e, w=widget, t=text:
                    self._schedule_show_tip(w, t), add="+")
        widget.bind("<Leave>", lambda e: self._schedule_hide_tip(), add="+")
        widget.bind("<ButtonPress>", lambda e: self._hide_tip_now(), add="+")

    def _schedule_show_tip(self, widget, text: str):
        self._cancel_tip_after()
        self._tip_widget = widget
        self._tip_text = text
        self._tip_after_id = self.after(250, self._show_tip_now)

    def _show_tip_now(self):
        self._tip_after_id = None
        w = self._tip_widget
        if w is None:
            return
        try:
            if not w.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        try:
            x = w.winfo_rootx() + 10
            y = w.winfo_rooty() + w.winfo_height() + 6
        except Exception:  # noqa: BLE001
            return

        if self._tip is None:
            self._tip = ctk.CTkToplevel(self)
            self._tip.wm_overrideredirect(True)
            self._tip.configure(fg_color=Color.LOG_BG)
            self._tip_label = ctk.CTkLabel(
                self._tip, text=self._tip_text, font=Font.TINY,
                text_color=Color.TEXT_PRIMARY, fg_color=Color.LOG_BG)
            self._tip_label.pack(padx=8, pady=3)

        try:
            self._tip_label.configure(text=self._tip_text)
            self._tip.geometry(f"+{x}+{y}")
            self._tip.deiconify()
            self._tip_visible = True
        except Exception:  # noqa: BLE001, S110
            pass

    def _schedule_hide_tip(self):
        self._cancel_tip_after()
        self._tip_after_id = self.after(120, self._hide_tip_now)

    def _hide_tip_now(self):
        self._tip_after_id = None
        if self._tip is not None:
            try:
                self._tip.withdraw()
            except Exception:  # noqa: BLE001, S110
                pass
        self._tip_visible = False

    def _cancel_tip_after(self):
        if self._tip_after_id is not None:
            try:
                self.after_cancel(self._tip_after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._tip_after_id = None

    def _on_parent_configure(self, _event=None):
        if self._tip_visible:
            self._hide_tip_now()

    def _on_parent_destroy(self, _event=None):
        self._cancel_tip_after()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
            self._tip = None

    # ------------------------------------------------------------------
    # 主区
    # ------------------------------------------------------------------
    def _build_main_area(self):
        wrap = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        wrap.grid(row=2, column=0, sticky="nsew",
                  padx=Size.PAD_PANEL, pady=(0, 6))
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_propagate(False)
        self._main_wrap = wrap

        self.editor = ctk.CTkTextbox(
            wrap, fg_color=Color.LOG_BG, text_color=Color.TEXT_PRIMARY,
            font=(Font.FAMILY, 13), corner_radius=8,
            border_width=0, wrap="word",
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.editor.place(in_=wrap, relx=0, rely=0,
                          relwidth=1.0, relheight=1.0)
        try:
            self.editor._textbox.configure(undo=True, maxundo=500)
        except Exception:  # noqa: BLE001, S110
            pass

        self.preview = ctk.CTkTextbox(
            wrap, fg_color=Color.SIDEBAR_BG, text_color=Color.TEXT_PRIMARY,
            font=(Font.FAMILY, 13), corner_radius=8,
            border_width=0, wrap="none",
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.preview.place(in_=wrap, relx=1.0, rely=0,
                           relwidth=1.0, relheight=1.0)
        self.preview.configure(state="disabled")

        self.preview._textbox.bind(
            "<Control-MouseWheel>", self._on_preview_ctrl_wheel)

        self._setup_preview_tags()

    def _on_preview_ctrl_wheel(self, event):
        try:
            self.preview._textbox.xview_scroll(
                int(-event.delta / 120), "units")
        except Exception:  # noqa: BLE001, S110
            pass
        return "break"

    def _build_footer(self):
        bar = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        bar.grid(row=3, column=0, sticky="ew",
                 padx=Size.PAD_PANEL, pady=(0, Size.PAD_PANEL))

        ctk.CTkButton(bar, text="导入 .md", width=90, height=28,
                      font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_PRIMARY,
                      command=self._on_import)\
            .pack(side="left", padx=(0, 6))

        ctk.CTkButton(bar, text="导出 .md", width=90, height=28,
                      font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_PRIMARY,
                      command=self._on_export)\
            .pack(side="left")

    # ------------------------------------------------------------------
    # 模式切换
    # ------------------------------------------------------------------
    def toggle_mode(self):
        if self._mode == "edit":
            self._switch_to_preview()
        else:
            self._switch_to_edit()

    def _on_mode_change(self, value: str):
        if value == "预览" and self._mode != "preview":
            self._switch_to_preview()
        elif value == "编辑" and self._mode != "edit":
            self._switch_to_edit()

    def _switch_to_preview(self):
        self._mode = "preview"
        self.toolbar.grid_remove()
        self._animate_to(1.0)
        self._schedule_render(immediate=True)

    def _switch_to_edit(self):
        self._mode = "edit"
        self._animate_to(0.0)
        self.toolbar.grid()
        try:
            self.editor.focus_set()
        except Exception:  # noqa: BLE001, S110
            pass

    def _animate_to(self, target: float, duration_ms: int = 280):
        if self._anim_after_id is not None:
            try:
                self.after_cancel(self._anim_after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._anim_after_id = None

        start = self._anim_progress
        if abs(start - target) < 1e-3:
            self._apply_progress(target)
            return

        self._anim_start = start
        self._anim_target = target
        self._anim_step = 0
        self._anim_steps = max(1, duration_ms // 16)
        self._anim_tick()

    def _anim_tick(self):
        self._anim_step += 1
        t = min(1.0, self._anim_step / self._anim_steps)
        t_eased = _ease_in_out_cubic(t)
        cur = self._anim_start + (self._anim_target - self._anim_start) * t_eased
        self._apply_progress(cur)

        if self._anim_step < self._anim_steps:
            self._anim_after_id = self.after(16, self._anim_tick)
        else:
            self._anim_after_id = None
            self._apply_progress(self._anim_target)

    def _apply_progress(self, p: float):
        self._anim_progress = p
        try:
            self.editor.place_configure(relx=-p, relwidth=1.0)
            self.preview.place_configure(relx=1.0 - p, relwidth=1.0)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _suspend_render(self):
        self._render_suspended = True
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._render_after_id = None

    def _resume_render(self):
        self._render_suspended = False
        if self._mode == "preview":
            self._schedule_render(immediate=True)

    # ------------------------------------------------------------------
    def get_markdown(self) -> str:
        return self.editor.get("1.0", "end").rstrip("\n")

    def set_markdown(self, text: str):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text or "")
        try:
            self.editor._textbox.edit_reset()
        except Exception:  # noqa: BLE001, S110
            pass

    def get_line_spacing(self) -> int:
        return self._line_spacing

    def set_line_spacing(self, value: int):
        self._line_spacing = max(-6, min(20, int(value)))
        try:
            self.spacing_slider.set(self._line_spacing)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _apply_format(self, key: str):
        if self._mode != "edit":
            self._switch_to_edit()
        tb = self.editor._textbox
        try:
            tb.edit_separator()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            handler = _ACTIONS.get(key)
            if handler:
                handler(self)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.editor.focus_set()
        except Exception:  # noqa: BLE001, S110
            pass
        self._schedule_render()

    def _wrap_selection(self, before: str, after: str = ""):
        tb = self.editor._textbox
        try:
            sel = tb.tag_ranges("sel")
        except Exception:  # noqa: BLE001
            sel = ()
        if sel:
            start, end = sel[0], sel[1]
            text = tb.get(start, end)
            tb.delete(start, end)
            tb.insert(start, before + text + (after or before))
        else:
            tb.insert("insert", before + (after or before))

    def _prefix_lines(self, prefix: str):
        tb = self.editor._textbox
        try:
            sel = tb.tag_ranges("sel")
        except Exception:  # noqa: BLE001
            sel = ()
        if sel:
            start, end = sel[0], sel[1]
        else:
            start = tb.index("insert linestart")
            end = tb.index("insert lineend")
        text = tb.get(start, end)
        new_text = "\n".join(prefix + line if line else prefix
                             for line in text.split("\n"))
        tb.delete(start, end)
        tb.insert(start, new_text)

    def _set_heading(self, level: int):
        tb = self.editor._textbox
        start = tb.index("insert linestart")
        end = tb.index("insert lineend")
        line = tb.get(start, end)
        stripped = line.lstrip("#").lstrip()
        prefix = "#" * level + " "
        new_line = prefix + stripped
        tb.delete(start, end)
        tb.insert(start, new_line)

    def _insert_block(self, block: str, wrap_with_newlines: bool = True):
        tb = self.editor._textbox
        content = block
        if wrap_with_newlines:
            content = "\n" + content + "\n"
        tb.insert("insert", content)

    def _act_h1(self): self._set_heading(1)
    def _act_h2(self): self._set_heading(2)
    def _act_h3(self): self._set_heading(3)
    def _act_bold(self): self._wrap_selection("**")
    def _act_italic(self): self._wrap_selection("*")
    def _act_strike(self): self._wrap_selection("~~")
    def _act_ul(self): self._prefix_lines("- ")
    def _act_ol(self): self._prefix_lines("1. ")
    def _act_quote(self): self._prefix_lines("> ")
    def _act_code(self): self._wrap_selection("`")

    def _act_link(self):
        tb = self.editor._textbox
        try:
            sel = tb.tag_ranges("sel")
        except Exception:  # noqa: BLE001
            sel = ()
        if sel:
            text = tb.get(sel[0], sel[1])
            tb.delete(sel[0], sel[1])
            tb.insert(sel[0], f"[{text}](url)")
        else:
            tb.insert("insert", "[链接文字](url)")

    def _act_image(self):
        self.editor._textbox.insert("insert", "![描述](图片地址)")

    def _act_codeblock(self):
        self._insert_block("```\n代码\n```")

    def _act_hr(self):
        self._insert_block("---")

    def _act_table(self):
        tbl = ("| 列1 | 列2 |\n"
               "| --- | --- |\n"
               "| 内容 | 内容 |")
        self._insert_block(tbl)

    def _act_undo(self):
        try:
            self.editor._textbox.edit_undo()
        except Exception:  # noqa: BLE001, S110
            pass

    def _act_redo(self):
        try:
            self.editor._textbox.edit_redo()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _on_modified(self, _event=None):
        tb = self.editor._textbox
        try:
            if tb.edit_modified():
                tb.edit_modified(False)
        except Exception:  # noqa: BLE001, S110
            pass
        if self._mode == "preview":
            self._schedule_render()

    def _schedule_render(self, immediate: bool = False):
        if self._render_suspended:
            return
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._render_after_id = None
        delay = 10 if immediate else 180
        self._render_after_id = self.after(delay, self._do_render)

    def _do_render(self):
        self._render_after_id = None
        if self._render_suspended:
            return
        if self._mode != "preview":
            return

        md_text = self.get_markdown()

        if not is_engine_available():
            tk_plain_text(self.preview._textbox,
                          "未安装 multimark，请执行：pip install multimark")
            return

        self._render_token += 1
        token = self._render_token

        def _on_result(html_body: str):
            self.after(0, lambda h=html_body, t=token:
                       self._apply_result(h, t))

        def _on_error(_err: str):
            self.after(0, lambda m=md_text, t=token:
                       self._apply_fallback(m, t))

        render_markdown(md_text, options=None,
                        on_result=_on_result, on_error=_on_error)

    def _apply_result(self, html_body: str, token: int):
        if token != self._render_token or self._mode != "preview":
            return
        html_to_tk(self.preview._textbox, html_body)

    def _apply_fallback(self, md_text: str, token: int):
        if token != self._render_token or self._mode != "preview":
            return
        tk_plain_text(self.preview._textbox, md_text)

    def _setup_preview_tags(self):
        if self._preview_tags_ready:
            return
        try:
            tb = self.preview._textbox
        except Exception:  # noqa: BLE001
            return
        _configure_render_tags(tb, self._line_spacing, base_size=13)
        self._preview_tags_ready = True

    # ------------------------------------------------------------------
    def _open_full_preview(self):
        md_text = self.get_markdown()

        if not is_engine_available():
            alert(self, "预览不可用",
                  "未安装 multimark 渲染库。\n\n"
                  "请执行：pip install multimark",
                  level="warn")
            return

        def _on_result(body_html: str):
            full = wrap_html(body_html, light=True)
            self.after(0, lambda h=full: self._launch_preview_window(h))

        def _on_error(_err: str):
            self.after(0, lambda m=md_text: self._launch_preview_fallback(m))

        render_markdown(md_text, options=None,
                        on_result=_on_result, on_error=_on_error)

    def _launch_preview_window(self, html: str):
        ok, err = open_preview_window(html)
        if not ok:
            alert(self, "预览失败", err, level="error")

    def _launch_preview_fallback(self, md_text: str):
        ok, err = open_preview_window(plain_text_html(md_text))
        if not ok:
            alert(self, "预览失败", err, level="error")

    # ------------------------------------------------------------------
    def _on_import(self):
        path = filedialog.askopenfilename(
            title="导入 Markdown",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt"),
                       ("所有文件", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return
        self.set_markdown(text)
        if self._mode == "preview":
            self._schedule_render(immediate=True)

    def _on_export(self):
        path = filedialog.asksaveasfilename(
            title="导出 Markdown",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt")],
            initialfile="changelog.md")
        if not path:
            return
        try:
            Path(path).write_text(self.get_markdown(), encoding="utf-8")
        except Exception:  # noqa: BLE001, S110
            pass


_ACTIONS: dict[str, Callable] = {
    "h1":        lambda ed: ed._act_h1(),
    "h2":        lambda ed: ed._act_h2(),
    "h3":        lambda ed: ed._act_h3(),
    "bold":      lambda ed: ed._act_bold(),
    "italic":    lambda ed: ed._act_italic(),
    "strike":    lambda ed: ed._act_strike(),
    "ul":        lambda ed: ed._act_ul(),
    "ol":        lambda ed: ed._act_ol(),
    "quote":     lambda ed: ed._act_quote(),
    "code":      lambda ed: ed._act_code(),
    "link":      lambda ed: ed._act_link(),
    "image":     lambda ed: ed._act_image(),
    "codeblock": lambda ed: ed._act_codeblock(),
    "hr":        lambda ed: ed._act_hr(),
    "table":     lambda ed: ed._act_table(),
    "undo":      lambda ed: ed._act_undo(),
    "redo":      lambda ed: ed._act_redo(),
}
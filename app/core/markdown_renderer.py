"""
markdown_renderer.py — Markdown 渲染引擎封装
------------------------------------------------
对外职责：
  1. render_markdown(md)               → HTML 字符串（multimark，异步）
  2. html_to_tk(widget, html)          → HTML → tk.Text（嵌入式预览用）
  3. tk_plain_text(widget, text)       → 纯文本降级
  4. wrap_html(body, light=True/False) → 完整 HTML 文档（弹窗用）
  5. open_preview_window(html)         → 独立窗口真渲染

未来替换引擎时，只改本模块。
"""

import os
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

from ..theme import MARKDOWN_CSS_DARK, MARKDOWN_CSS_LIGHT

try:
    from multimark import markdown_to_html as _md_to_html_imported
    _MULTIMARK_AVAILABLE = True
except ImportError:
    _md_to_html_imported = None
    _MULTIMARK_AVAILABLE = False


_md_to_html: Callable[..., str] | None = _md_to_html_imported

# 弹窗临时文件（覆盖写）
_PREVIEW_PREFIX = "pulses_easier_preview_"
_PREVIEW_FILENAME = _PREVIEW_PREFIX + "current.html"  # 兼容旧名


# ----------------------------------------------------------------------
# 默认渲染选项
# ----------------------------------------------------------------------
_DEFAULT_EXTENSIONS = ["table", "strikethrough", "autolink", "tasklist"]

_DEFAULT_OPTIONS = {
    "extensions": list(_DEFAULT_EXTENSIONS),
    "smart": True,
    "hardbreaks": False,
    "unsafe": False,
    "sourcepos": False,
}

CONFIGURABLE_OPTIONS: dict[str, str] = {
    "table":         "表格",
    "strikethrough": "删除线",
    "autolink":      "自动链接",
    "tasklist":      "任务列表",
    "smart":         "智能标点",
    "hardbreaks":    "软换行转硬换行",
    "unsafe":        "允许原始 HTML",
}


def is_engine_available() -> bool:
    return _MULTIMARK_AVAILABLE and _md_to_html is not None


def _build_kwargs(options: dict) -> dict:
    kw: dict[str, Any] = {}
    exts = options.get("extensions", _DEFAULT_EXTENSIONS)
    if exts:
        kw["extensions"] = list(exts)
    for key in ("smart", "hardbreaks", "unsafe", "sourcepos"):
        if options.get(key):
            kw[key] = True
    return kw


# ----------------------------------------------------------------------
# Markdown → HTML（异步）
# ----------------------------------------------------------------------
def render_markdown(md_text: str,
                    options: dict | None = None,
                    on_result: Callable[[str], None] | None = None,
                    on_error: Callable[[str], None] | None = None):
    def _worker():
        engine = _md_to_html
        if engine is None:
            if on_error:
                on_error("缺少 multimark，请执行：pip install multimark")
            return
        try:
            merged = dict(_DEFAULT_OPTIONS)
            if options:
                merged.update(options)
            kwargs = _build_kwargs(merged)
            html = engine(md_text or "", **kwargs)
            if on_result:
                on_result(html)
        except Exception as e:  # noqa: BLE001
            if on_error:
                on_error(f"渲染失败：{e}")

    threading.Thread(target=_worker, daemon=True).start()

# ----------------------------------------------------------------------
# 同步渲染（供预渲染线程使用）
# ----------------------------------------------------------------------
def render_markdown_sync(md_text: str,
                         options: dict | None = None
                         ) -> tuple[bool, str]:
    """
    同步渲染 Markdown → HTML body。
    返回 (ok, html 或错误信息)。
    仅用于后台线程；不要在主线程大量调用。
    """
    engine = _md_to_html
    if engine is None:
        return False, "缺少 multimark，请执行：pip install multimark"
    try:
        merged = dict(_DEFAULT_OPTIONS)
        if options:
            merged.update(options)
        kwargs = _build_kwargs(merged)
        html = engine(md_text or "", **kwargs)
        return True, html
    except Exception as e:  # noqa: BLE001
        return False, f"渲染失败：{e}"
    
# ----------------------------------------------------------------------
# 完整 HTML 文档包装
# ----------------------------------------------------------------------
def wrap_html(body_html: str, light: bool = True) -> str:
    """
    把 body 片段包成完整 HTML 文档。
    light=True 用 GitHub 风格浅色（弹窗用）；
    light=False 用深色（备用）。
    """
    css = MARKDOWN_CSS_LIGHT if light else MARKDOWN_CSS_DARK
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{css}</style></head><body>"
        f"{body_html}"
        "</body></html>"
    )


# ----------------------------------------------------------------------
# HTML → tk.Text（嵌入式预览）
# ----------------------------------------------------------------------
class _HTMLToTk(HTMLParser):
    """
    把 HTML 写入 tk.Text，用 tag 表达样式。
    只负责 "HTML → tk.Text"，不做 Markdown 解析。
    """

    _SKIP: ClassVar[set[str]] = {"head", "style", "script"}

    _INLINE: ClassVar[dict[str, str]] = {
        "strong": "bold", "b": "bold",
        "em": "italic", "i": "italic",
        "del": "strike", "s": "strike", "strike": "strike",
        "code": "code",
    }

    _BLOCK: ClassVar[dict[str, str]] = {
        "h1": "h1", "h2": "h2", "h3": "h3",
        "h4": "h4", "h5": "h5", "h6": "h6",
        "p": "p", "blockquote": "quote",
    }

    def __init__(self, text_widget):
        super().__init__(convert_charrefs=True)
        self.text = text_widget
        self._stack: list[str] = []
        self._skip_depth = 0
        self._in_pre = False
        self._link_href = ""
        self._pending_newline = 0

        self._ol_counter_stack: list[int] = []

        self._table_rows: list[list[str]] = []
        self._table_header_count = 0
        self._current_row: list[str] = []
        self._current_cell: list[str] = []
        self._in_cell = False

    def _write(self, text: str, extra_tags: list[str] | None = None):
        if text == "":
            return
        try:
            start = self.text.index("end-1c")
        except Exception:  # noqa: BLE001
            start = "1.0"
        self.text.insert("end", text)
        try:
            end = self.text.index("end-1c")
        except Exception:  # noqa: BLE001
            return
        tags = list(self._stack)
        if extra_tags:
            tags.extend(extra_tags)
        for tg in tags:
            if not tg:
                continue
            try:
                self.text.tag_add(tg, start, end)
            except Exception:  # noqa: BLE001, S110
                pass

    def _write_tagged(self, text: str, tag: str):
        if text == "":
            return
        try:
            start = self.text.index("end-1c")
        except Exception:  # noqa: BLE001
            start = "1.0"
        self.text.insert("end", text)
        try:
            end = self.text.index("end-1c")
        except Exception:  # noqa: BLE001
            return
        if tag:
            try:
                self.text.tag_add(tag, start, end)
            except Exception:  # noqa: BLE001, S110
                pass

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in self._SKIP:
            self._skip_depth += 1
            return

        if tag == "pre":
            self._in_pre = True
            self._stack.append("pre")
            self._request_newline(1)
            return

        # 表格：只收集
        if tag == "table":
            self._table_rows = []
            self._table_header_count = 0
            self._request_newline(1)
            return
        if tag in ("thead", "tbody"):
            return
        if tag == "tr":
            self._current_row = []
            return
        if tag == "th":
            self._current_cell = []
            self._in_cell = True
            self._stack.append("table_header")
            return
        if tag == "td":
            self._current_cell = []
            self._in_cell = True
            self._stack.append("table_cell")
            return

        # 列表
        if tag == "ul":
            self._stack.append("ul")
            self._request_newline(1)
            return
        if tag == "ol":
            self._stack.append("ol")
            self._ol_counter_stack.append(0)
            self._request_newline(1)
            return
        if tag == "li":
            self._flush_newline()
            if self._ol_counter_stack:
                self._ol_counter_stack[-1] += 1
                marker = f"{self._ol_counter_stack[-1]}. "
            else:
                marker = "• "
            self._write_tagged(marker, "li_marker")
            self._stack.append("li")
            return

        # 引用
        if tag == "blockquote":
            self._stack.append("quote")
            self._request_newline(1)
            return

        if tag in self._BLOCK:
            self._request_newline(1)
            self._stack.append(tag)
            return

        if tag == "br":
            self._flush_newline()
            self.text.insert("end", "\n")
            return

        if tag == "hr":
            self._request_newline(1)
            self._flush_newline()
            self._write_tagged("─" * 40, "hr")
            self._request_newline(1)
            return

        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href":
                    href = v or ""
                    break
            self._link_href = href
            return

        if tag in self._INLINE:
            self._stack.append(self._INLINE[tag])
            return

        if tag == "img":
            alt = ""
            for k, v in attrs:
                if k.lower() == "alt":
                    alt = v or ""
                    break
            self._flush_newline()
            self._write(f"[图片: {alt or '未命名'}]", ["img"])
            return

        # 任务列表
        if tag == "input":
            input_type = ""
            checked = False
            for k, v in attrs:
                if k.lower() == "type":
                    input_type = v or ""
                if k.lower() == "checked":
                    checked = True
            if input_type == "checkbox":
                self._write_tagged("[✓] " if checked else "[ ] ",
                                   "li_marker")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        if tag == "pre":
            if self._stack and self._stack[-1] == "pre":
                self._stack.pop()
            self._in_pre = False
            self._request_newline(1)
            return

        if tag == "table":
            self._flush_table()
            self._request_newline(1)
            return
        if tag == "tr":
            self._table_rows.append(self._current_row)
            self._current_row = []
            return
        if tag == "th":
            if "table_header" in self._stack:
                self._stack.remove("table_header")
            self._current_row.append("".join(self._current_cell).strip())
            self._current_cell = []
            self._in_cell = False
            self._table_header_count += 1
            return
        if tag == "td":
            if "table_cell" in self._stack:
                self._stack.remove("table_cell")
            self._current_row.append("".join(self._current_cell).strip())
            self._current_cell = []
            self._in_cell = False
            return

        if tag == "ul":
            if self._stack and self._stack[-1] == "ul":
                self._stack.pop()
            self._request_newline(1)
            return
        if tag == "ol":
            if self._stack and self._stack[-1] == "ol":
                self._stack.pop()
            if self._ol_counter_stack:
                self._ol_counter_stack.pop()
            self._request_newline(1)
            return
        if tag == "li":
            if "li" in self._stack:
                self._stack.remove("li")
            self._request_newline(1)
            return

        if tag == "blockquote":
            if "quote" in self._stack:
                self._stack.remove("quote")
            self._request_newline(1)
            return

        if tag == "a":
            if self._link_href:
                self._flush_newline()
                self._write(f" ({self._link_href})", ["link_url"])
                self._link_href = ""
            return

        if tag in self._INLINE:
            name = self._INLINE[tag]
            if name in self._stack:
                self._stack.remove(name)
            return

        if tag in self._BLOCK:
            if tag in self._stack:
                try:
                    self._stack.remove(tag)
                except ValueError:
                    pass
            self._request_newline(1)

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        if self._in_cell:
            self._current_cell.append(data)
            return
        if self._in_pre:
            self._flush_newline()
            self._write(data)
            return
        if "quote" in self._stack:
            text = " ".join(data.split())
            if text:
                self._flush_newline()
                self._write_tagged("│ ", "quote_mark")
                self._write(text)
            return
        text = " ".join(data.split())
        if text:
            self._flush_newline()
            self._write(text)

    def _flush_table(self):
        if not self._table_rows:
            return
        import unicodedata

        def dw(s):
            w = 0
            for ch in s:
                w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
            return w

        n_cols = max(len(r) for r in self._table_rows)
        for row in self._table_rows:
            while len(row) < n_cols:
                row.append("")

        col_widths = [0] * n_cols
        for row in self._table_rows:
            for i, cell in enumerate(row):
                w = dw(cell)
                col_widths[i] = max(col_widths[i], w)

        header_written = False
        for r_idx, row in enumerate(self._table_rows):
            self._flush_newline()
            for i, cell in enumerate(row):
                is_last = (i == n_cols - 1)
                text = cell + " " * max(0, col_widths[i] - dw(cell))
                is_header = r_idx < 1 and self._table_header_count > 0
                tag = "table_header" if is_header else "table_cell"
                self._write_tagged(text, tag)
                if not is_last:
                    self._write_tagged(" │ ", "table_sep")
            if (not header_written and r_idx == 0
                    and self._table_header_count > 0):
                self._request_newline(1)
                self._flush_newline()
                rule_len = sum(col_widths) + 3 * (n_cols - 1)
                self._write_tagged("─" * rule_len, "table_rule")
                header_written = True
            self._request_newline(1)
        self._request_newline(1)

    def _request_newline(self, count: int):
        self._pending_newline = max(self._pending_newline, count)

    def _flush_newline(self):
        if self._pending_newline <= 0:
            return
        empty = True
        try:
            empty = self.text.compare("end-1c", "==", "1.0")
        except Exception:  # noqa: BLE001
            pass
        if not empty:
            try:
                if self.text.get("end-2c", "end-1c") == "\n":
                    self._pending_newline = 0
                    return
            except Exception:  # noqa: BLE001, S110
                pass
        self.text.insert("end", "\n" * self._pending_newline)
        self._pending_newline = 0


def html_to_tk(tk_text_widget, html: str):
    """把 HTML 渲染到 tk.Text（清空后重写）。异常时静默失败。"""
    tb = tk_text_widget
    try:
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        parser = _HTMLToTk(tb)
        parser.feed(html)
        parser.close()
        tb.configure(state="disabled")
    except Exception:  # noqa: BLE001
        try:
            tb.configure(state="disabled")
        except Exception:  # noqa: BLE001, S110
            pass


def tk_plain_text(tk_text_widget, text: str):
    """降级：纯文本写入 tk.Text"""
    tb = tk_text_widget
    try:
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.insert("1.0", text or "")
        tb.configure(state="disabled")
    except Exception:  # noqa: BLE001
        try:
            tb.configure(state="disabled")
        except Exception:  # noqa: BLE001, S110
            pass


# ----------------------------------------------------------------------
# 独立预览窗口（真渲染）
# ----------------------------------------------------------------------
def open_preview_window(html: str) -> tuple[bool, str]:
    """
    打开独立预览窗口，使用真浏览器内核（WebView2）渲染。
    缺 pywebview 时降级到系统默认浏览器。
    """
    # 唯一文件名：固定名会被连续两次预览互相覆盖（后一次写入时
    # 前一次的子进程可能还在读这个文件）
    token = uuid.uuid4().hex[:12]
    try:
        html_path = Path(tempfile.gettempdir()) / f"{_PREVIEW_PREFIX}{token}.html"
        html_path.write_text(html, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return False, f"写入预览文件失败：{e}"

    try:
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.core.preview_worker", str(html_path)],
            creationflags=creation_flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _watch_preview(proc, html_path)
        return True, ""
    except Exception as e:  # noqa: BLE001
        try:
            import webbrowser
            webbrowser.open(html_path.as_uri())
            return True, ""
        except Exception as e2:  # noqa: BLE001
            return False, f"启动预览失败：{e}；浏览器兜底失败：{e2}"


def _watch_preview(proc, html_path: Path):
    """
    看着预览子进程：非 0 退出（pywebview 起不来等）时自动用系统浏览器
    兜底打开，不再"静默什么都不发生"。子进程正常退出后删掉临时文件。
    """
    def _worker():
        try:
            rc = proc.wait(timeout=3600)
        except Exception:  # noqa: BLE001
            return
        if rc != 0:
            try:
                import webbrowser
                webbrowser.open(html_path.as_uri())
                return          # 浏览器可能还在读，保留文件
            except Exception:  # noqa: BLE001
                pass
        try:
            html_path.unlink()
        except Exception:  # noqa: BLE001, S110
            pass

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:  # noqa: BLE001, S110
        pass


# ----------------------------------------------------------------------
# 纯文本包成 HTML（弹窗降级用）
# ----------------------------------------------------------------------
def plain_text_html(text: str) -> str:
    escaped = (text or "").replace("&", "&amp;").replace(
        "<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("\n", "<br>")
    return wrap_html(f"<p>{escaped}</p>", light=True)
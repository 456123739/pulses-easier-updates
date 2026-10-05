"""
preview_worker.py — 独立预览窗口子进程入口
------------------------------------------------
在独立进程中启动 pywebview，避免与 Tkinter 主循环冲突。
子进程中 pywebview 是主线程，不会被 "must be run on a main thread" 拦截。

用法：
    python -m app.core.preview_worker <html_file>

行为：
    1. 读取 HTML 文件
    2. 优先用 pywebview 打开（独立窗口，WebView2 真渲染）
    3. pywebview 不可用 / 启动失败时，降级到系统默认浏览器
"""

import sys
from pathlib import Path


def _open_with_browser(html_path: Path) -> None:
    """降级：用系统默认浏览器打开"""
    try:
        import webbrowser
        webbrowser.open(html_path.as_uri())
    except Exception:  # noqa: BLE001, S110
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.core.preview_worker <html_file>",
              file=sys.stderr)
        return 1

    html_path = Path(sys.argv[1])
    if not html_path.is_file():
        print(f"file not found: {html_path}", file=sys.stderr)
        return 1

    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"read failed: {e}", file=sys.stderr)
        return 1

    try:
        import webview
    except ImportError:
        _open_with_browser(html_path)
        return 0

    try:
        webview.create_window(
            "更新日志预览",
            html=html,
            width=1200,
            height=800,
            resizable=True,
        )
        webview.start()
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"webview failed: {e}", file=sys.stderr)
        _open_with_browser(html_path)
        return 0


if __name__ == "__main__":
    sys.exit(main())
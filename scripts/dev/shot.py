#!/usr/bin/env python3
"""X11 截图（Tk 版界面用）：DISPLAY 指向 Xvfb 或真实屏幕。

用法：
    DISPLAY=:99 .venv/bin/python scripts/dev/shot.py out.png [x y w h]
不给区域就抓整屏。依赖 Pillow 的 X11 支持（Pillow>=9.2）。
"""
from __future__ import annotations

import os
import sys

from PIL import ImageGrab


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out = sys.argv[1]
    disp = os.environ.get("DISPLAY", ":0")
    box = None
    if len(sys.argv) >= 6:
        x, y, w, h = (int(v) for v in sys.argv[2:6])
        box = (x, y, x + w, y + h)
    img = ImageGrab.grab(xdisplay=disp, bbox=box)
    img.save(out)
    print(f"saved {out} {img.size[0]}x{img.size[1]} from {disp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
paths.py — 资源目录定位（源码运行 / PyInstaller 打包后都能用）
------------------------------------------------
打包成 exe 之后有两种布局，取资源的规则不同：

  * 源码运行：`<项目根>/assets`
  * 打包运行：PyInstaller 会把资源放进 `_MEIPASS`（6.x 是 `<exe目录>/_internal`），
    但**玩家看得见的 assets 在 exe 同级**。这里优先用 exe 同级那份：
    一来 zip 里"软件 + assets + logo"一目了然，二来玩家想换 logo 直接覆盖
    就生效（不用重新打包）。
"""

import sys
from pathlib import Path


def project_root() -> Path:
    """项目根目录（源码运行）。"""
    return Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """可执行文件所在目录（冻结环境）；源码运行时返回项目根。"""
    if is_frozen():
        try:
            return Path(sys.executable).resolve().parent
        except Exception:  # noqa: BLE001
            return Path.cwd()
    return project_root()


def bundle_dir() -> Path:
    """PyInstaller 解包目录（含打包进去的资源）；源码运行时是项目根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return project_root()


def assets_dir() -> Path:
    """
    资源目录。冻结时优先 exe 同级的 assets/（玩家可见可改），
    没有才退回打包进去的那份。
    """
    if is_frozen():
        side = exe_dir() / "assets"
        if side.is_dir():
            return side
    bundled = bundle_dir() / "assets"
    if bundled.is_dir():
        return bundled
    return project_root() / "assets"


def asset(name: str) -> Path:
    """取具体资源文件路径（不存在也照样返回，调用方自己判断）。"""
    return assets_dir() / name

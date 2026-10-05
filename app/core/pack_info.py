"""
pack_info.py — 整合包信息读取
------------------------------------------------
读取名称、版本、图标、模组数量、最后修改时间。
所有读取失败均返回回退值，不抛异常。

图标查找优先级：
  1. <root>/PCL/Logo.png（PCL 专用图标）
  2. <root>/icon.png、pack.png、logo.png、modpack.png、instance.png
  3. <root>/assets 或 <root>/icons 下的同名文件
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 名称来源文件（按优先级）
_NAME_FILES = ("modpack_name.txt", "name.txt")

# 图标来源文件名（按优先级，根目录）
_ICON_FILES = ("icon.png", "pack.png", "logo.png",
               "modpack.png", "instance.png")

# PCL 专用图标相对路径
_PCL_ICON_REL = ("PCL", "Logo.png")


@dataclass
class ModpackInfo:
    name: str = "未命名整合包"
    version: str = "未知"
    path: Path = field(default_factory=Path)
    mod_count: int = 0
    last_modified: float = 0.0
    icon_path: Path | None = None

    # 兼容旧字段名
    @property
    def mods_count(self) -> int:
        return self.mod_count


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def read_modpack_info(modpack_path: str | Path) -> ModpackInfo:
    """
    读取整合包信息。任何字段读取失败都返回回退值，不抛异常。
    """
    root = Path(modpack_path)
    info = ModpackInfo(path=root)

    info.name = _read_name(root) or root.name or "未命名整合包"
    info.version = _read_version(root, info.name)
    info.icon_path = _find_icon(root)
    info.mod_count = _count_mods(root / "mods")

    try:
        info.last_modified = root.stat().st_mtime
    except OSError:
        info.last_modified = 0.0

    return info


# ----------------------------------------------------------------------
# 名称
# ----------------------------------------------------------------------
def _read_name(root: Path) -> str | None:
    for fname in _NAME_FILES:
        p = root / fname
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0].strip()
            except (OSError, UnicodeDecodeError):
                continue

    cfg = root / "instance.cfg"
    if cfg.is_file():
        name = _parse_ini_key(cfg, "name")
        if name:
            return name

    return None


# ----------------------------------------------------------------------
# 版本
# ----------------------------------------------------------------------
def _read_version(root: Path, fallback_name: str) -> str:
    for fname in ("version.json", "manifest.json"):
        p = root / fname
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                v = _extract_version_from_json(data)
                if v:
                    return str(v)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue

    mmc = root / "mmc-pack.json"
    if mmc.is_file():
        try:
            data = json.loads(mmc.read_text(encoding="utf-8"))
            v = _extract_version_from_mmc(data)
            if v:
                return str(v)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    cfg = root / "instance.cfg"
    if cfg.is_file():
        v = (_parse_ini_key(cfg, "IntendedVersion")
             or _parse_ini_key(cfg, "version"))
        if v:
            return v

    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", fallback_name or root.name)
    if m:
        return m.group(1)

    return "未知"


def _extract_version_from_json(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("version", "packVersion", "modpackVersion", "id"):
        if key in data and isinstance(data[key], str):
            return data[key]
    v = data.get("version")
    if isinstance(v, dict):
        for key in ("number", "name", "id"):
            if isinstance(v.get(key), str):
                return v[key]
    mc = data.get("minecraft")
    if isinstance(mc, dict) and isinstance(mc.get("version"), str):
        return mc["version"]
    return None


def _extract_version_from_mmc(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    components = data.get("components")
    if isinstance(components, list):
        for comp in components:
            if isinstance(comp, dict) and comp.get("uid") == "net.minecraft":
                v = comp.get("version")
                if isinstance(v, str):
                    return v
    return None


def _parse_ini_key(path: Path, key: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip().lower() == key.lower():
                    return v.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None


# ----------------------------------------------------------------------
# 图标
# ----------------------------------------------------------------------
def _find_icon(root: Path) -> Path | None:
    """
    查找整合包图标，优先级：
      1. PCL 专用图标 <root>/PCL/Logo.png
      2. 根目录常见图标名
      3. assets / icons 子目录下的常见图标名
    """
    # 1) PCL 图标
    pcl_icon = root.joinpath(*_PCL_ICON_REL)
    if pcl_icon.is_file():
        return pcl_icon

    # 2) 根目录
    for fname in _ICON_FILES:
        p = root / fname
        if p.is_file():
            return p

    # 3) 子目录
    for subdir in ("assets", "icons"):
        d = root / subdir
        if not d.is_dir():
            continue
        for fname in _ICON_FILES:
            p = d / fname
            if p.is_file():
                return p

    return None


# ----------------------------------------------------------------------
# 模组数量
# ----------------------------------------------------------------------
def _count_mods(mods_dir: Path) -> int:
    if not mods_dir.is_dir():
        return 0
    try:
        return sum(
            1 for e in mods_dir.iterdir()
            if e.is_file() and e.suffix.lower() == ".jar"
        )
    except OSError:
        return 0


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def format_modified(ts: float) -> str:
    """时间戳 → 可读字符串"""
    if not ts:
        return "未知"
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# 兼容旧字段名
PackInfo = ModpackInfo
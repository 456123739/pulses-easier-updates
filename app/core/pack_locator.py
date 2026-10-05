"""
pack_locator.py — 整合包定位
------------------------------------------------
从任意文件/文件夹路径向上查找整合包根目录。

判定规则（满足任一）：
  1. 目录内同时存在 mods 和 config 两个文件夹
  2. 目录内存在以 -native 结尾的 .jar 文件

异常情况返回 None，并通过 LocateResult 附加原因。
"""

import os
from pathlib import Path
from typing import NamedTuple

# 最大向上查找层数（可配置）
MAX_DEPTH = 8


class LocateResult(NamedTuple):
    """定位结果：路径 + 失败原因（成功时 reason 为空）"""
    path: Path | None
    reason: str


# ----------------------------------------------------------------------
# 目录特征判定
# ----------------------------------------------------------------------
def _is_modpack_dir_from_path(dir_path: Path) -> bool:
    """
    判定 dir_path 是否为整合包根目录。
    单层只读一次目录，用集合运算判定。
    """
    try:
        names_lower: set[str] = set()
        has_native_jar = False
        with os.scandir(dir_path) as it:
            for sub in it:
                try:
                    name_lower = sub.name.lower()
                except OSError:
                    continue
                names_lower.add(name_lower)
                if name_lower.endswith(".jar") and "-native" in name_lower:
                    has_native_jar = True
    except (PermissionError, OSError):
        return False

    if "mods" in names_lower and "config" in names_lower:
        return True
    return has_native_jar


# ----------------------------------------------------------------------
# 主函数
# ----------------------------------------------------------------------
def locate_modpack(path: str | Path,
                   max_depth: int = MAX_DEPTH) -> str | None:
    """
    从 path 出发向上逐级查找整合包根目录。

    参数
    ----
    path        任意文件或文件夹路径
    max_depth   最大向上查找层数，默认 8

    返回
    ----
    整合包根目录的绝对路径（str），失败返回 None
    """
    result = locate_modpack_ex(path, max_depth)
    return str(result.path) if result.path else None


def locate_modpack_ex(path: str | Path,
                      max_depth: int = MAX_DEPTH) -> LocateResult:
    """
    与 locate_modpack 相同，但返回 LocateResult，
    额外携带失败原因，便于日志与调试。
    """
    # 1) 路径标准化
    try:
        p = Path(path)
    except (TypeError, ValueError) as e:
        return LocateResult(None, f"路径无效：{e}")

    try:
        p = p.expanduser()
        try:
            p = p.resolve(strict=False)
        except (OSError, RuntimeError):
            pass
    except Exception as e:  # noqa: BLE001
        return LocateResult(None, f"路径标准化失败：{e}")

    # 2) 路径存在性检查
    if not p.exists():
        return LocateResult(None, f"路径不存在：{p}")

    # 3) 判定起点：文件取其父目录，目录用自身
    try:
        if p.is_file():
            start = p.parent
        elif p.is_dir():
            start = p
        else:
            return LocateResult(None, f"路径不是文件或目录：{p}")
    except OSError as e:
        return LocateResult(None, f"无法访问路径：{e}")

    # 4) 向上逐级查找
    current = start
    for _depth in range(max_depth + 1):
        if _is_modpack_dir_from_path(current):
            return LocateResult(current, "")

        if current.parent == current:
            break
        current = current.parent

    return LocateResult(None, f"向上 {max_depth} 层未找到整合包根目录")


# ----------------------------------------------------------------------
# 兼容旧接口
# ----------------------------------------------------------------------
def locate_pack_root(path: str | Path) -> Path | None:
    """旧接口兼容：返回 Path 或 None"""
    r = locate_modpack_ex(path)
    return r.path
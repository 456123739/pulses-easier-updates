"""
pack_detector.py — 拖入 ZIP 类型识别
------------------------------------------------
玩家端拖入的 ZIP 可能是两种之一：
  - 导出包：启动器导出的整合包 ZIP（MR 格式）
  - 更新包：Pulses Easier 生成的 .eapack

判定依据：
  - 有 ea_settings.json 且 ea_manifest.json → 更新包
  - 有 modrinth.index.json → 导出包（overrides/ 可选）
  - 都不匹配 → 未知

预留扩展：后续新增格式只需在 _detect 里加分支。
"""

import zipfile
from enum import Enum
from pathlib import Path


class PackKind(str, Enum):
    EXPORT = "export"
    UPDATE = "update"
    UNKNOWN = "unknown"


# 特征文件名
_INDEX = "modrinth.index.json"
_OVERRIDES_PREFIX = "overrides/"
_SETTINGS = "ea_settings.json"
_MANIFEST = "ea_manifest.json"


def detect_pack_kind(zip_path: Path) -> tuple[PackKind, str]:
    """
    检测 ZIP 类型。
    返回 (kind, error_message)，未知时 error_message 说明原因。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return PackKind.UNKNOWN, f"文件不存在：{zip_path}"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            return _detect(names)
    except zipfile.BadZipFile:
        return PackKind.UNKNOWN, "不是有效的 ZIP 文件"
    except Exception as e:  # noqa: BLE001
        return PackKind.UNKNOWN, f"读取失败：{e}"


def _detect(names: list[str]) -> tuple[PackKind, str]:
    has_settings = _SETTINGS in names
    has_manifest = _MANIFEST in names
    has_index = _INDEX in names or any(
        n.endswith("/" + _INDEX) for n in names)
    has_overrides = any(n.startswith(_OVERRIDES_PREFIX) for n in names)

    # 更新包优先：eapack 里也可能保留 index
    if has_settings and has_manifest:
        return PackKind.UPDATE, ""

    # 导出包
    if has_index:
        # overrides 不一定存在（有些启动器导出包把内容直接放根）
        return PackKind.EXPORT, ""

    # 提示信息更具体
    if has_overrides:
        return PackKind.UNKNOWN, "缺少 modrinth.index.json，无法识别"
    return PackKind.UNKNOWN, "缺少 modrinth.index.json 或 eapack 元数据"
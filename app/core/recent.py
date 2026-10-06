"""
recent.py — 最近打开的整合包记录
------------------------------------------------
记录用户最近定位过的整合包路径，供侧边栏"最近打开"列表使用。
存储位置：~/.pulses_easier/recent.json
上限 8 条，去重，最新在前。
"""

import json
from pathlib import Path

# 存储路径
_CONFIG_DIR = Path.home() / ".pulses_easier"
_RECENT_FILE = _CONFIG_DIR / "recent.json"
_MAX_ITEMS = 8


def _ensure_dir():
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001, S110
        pass


def load_recent() -> list[Path]:
    """读取最近列表，失败返回空列表"""
    try:
        if not _RECENT_FILE.is_file():
            return []
        data = json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [Path(p) for p in data if isinstance(p, str)]
    except Exception:  # noqa: BLE001
        return []


def _save(paths: list[Path]):
    _ensure_dir()
    try:
        _RECENT_FILE.write_text(
            json.dumps([str(p) for p in paths], ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass


def add_recent(pack_root: Path) -> list[Path]:
    """
    将 pack_root 加入最近列表（去重，置顶，截断到上限）。
    返回更新后的列表。
    """
    pack_root = Path(pack_root)
    try:
        resolved = str(pack_root.resolve())
    except Exception:  # noqa: BLE001
        resolved = str(pack_root)

    items = load_recent()
    # 去重
    items = [p for p in items if str(p) != resolved]
    items.insert(0, pack_root)
    items = items[:_MAX_ITEMS]
    _save(items)
    return items


def remove_recent(pack_root: Path) -> list[Path]:
    """从最近列表移除某项，返回更新后的列表"""
    try:
        resolved = str(Path(pack_root).resolve())
    except Exception:  # noqa: BLE001
        resolved = str(pack_root)
    items = [p for p in load_recent() if str(p) != resolved]
    _save(items)
    return items


def clear_recent():
    """清空最近列表"""
    _save([])
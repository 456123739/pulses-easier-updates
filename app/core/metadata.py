"""
metadata.py — 更新包元数据读写
------------------------------------------------
更新包（ZIP）内嵌 meta.json，保存版本、推荐策略、推荐勾选、更新日志。
"""

import json
from datetime import datetime
from pathlib import Path

from ..config import (META_FILENAME, META_KEY_VERSION, META_KEY_STRATEGIES,
                      META_KEY_CHECKED, META_KEY_CHANGELOG, META_KEY_CREATED_AT)


def build_meta(version: str,
               checked: dict, strategies: dict,
               changelog_md: str) -> dict:
    """构造元数据字典（键统一转字符串，便于 JSON 序列化）"""
    return {
        META_KEY_VERSION:    version,
        META_KEY_STRATEGIES: {k.value: v.value for k, v in strategies.items()},
        META_KEY_CHECKED:    {k.value: bool(v) for k, v in checked.items()},
        META_KEY_CHANGELOG:  changelog_md,
        META_KEY_CREATED_AT: datetime.now().isoformat(timespec="seconds"),
    }


def save_meta(meta: dict, path: Path):
    path = Path(path)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load_meta(path: Path) -> dict | None:
    """从 meta.json 读取，失败返回 None"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_meta_from_zip(zip_path: Path) -> dict | None:
    """从更新包 ZIP 中读取 meta.json"""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            with zf.open(META_FILENAME) as f:
                return json.loads(f.read().decode("utf-8"))
    except Exception:
        return None
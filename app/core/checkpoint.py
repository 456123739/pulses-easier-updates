"""
checkpoint.py — 更新过程检查点
------------------------------------------------
在更新的每个阶段（新增/替换/删除/校验）写入检查点，
中途失败后可从检查点恢复。

存储位置：<db>/cache/checkpoints/<pack_hash>.json
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import database as db


def _pack_hash(pack_root: Path) -> str:
    try:
        s = str(Path(pack_root).resolve())
    except Exception:  # noqa: BLE001
        s = str(pack_root)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _checkpoint_dir() -> Path | None:
    db_root = db.get_db_path()
    if db_root is None:
        return None
    d = db_root / "cache" / "checkpoints"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return None
    return d


@dataclass
class Checkpoint:
    pack_root: str
    stage: str                        # scan/download/apply/delete/verify
    done: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    new_root: str = ""
    updated_at: float = 0.0
    failed: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def save_checkpoint(cp: Checkpoint) -> bool:
    d = _checkpoint_dir()
    if d is None:
        return False
    cp.updated_at = time.time()
    try:
        path = d / f"{_pack_hash(Path(cp.pack_root))}.json"
        path.write_text(
            json.dumps(cp.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def load_checkpoint(pack_root: Path) -> Checkpoint | None:
    d = _checkpoint_dir()
    if d is None:
        return None
    try:
        path = d / f"{_pack_hash(pack_root)}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Checkpoint(
            pack_root=data.get("pack_root", str(pack_root)),
            stage=data.get("stage", ""),
            done=list(data.get("done", [])),
            remaining=list(data.get("remaining", [])),
            new_root=data.get("new_root", ""),
            updated_at=float(data.get("updated_at", 0.0)),
            failed=list(data.get("failed", [])),
        )
    except Exception:  # noqa: BLE001
        return None


def clear_checkpoint(pack_root: Path) -> bool:
    d = _checkpoint_dir()
    if d is None:
        return False
    try:
        path = d / f"{_pack_hash(pack_root)}.json"
        if path.is_file():
            path.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False
"""
checkpoint.py — 更新过程检查点
------------------------------------------------
在更新的每个阶段（替换/写入/删除）写入检查点。

用途（v0.5.0 起收窄）：
  **只用来判断"上一次更新是否正常结束"**：进程被强杀时检查点会留在
  `stage=apply/copy/delete`，下次启动提示用户重新拖入同一个更新包
  （向前重跑一定收敛）。不做"回滚上次更新"——那需要假设"更新后用户
  什么都没改过"，无法验证，反而会把版本搞乱。

存储位置：<db>/cache/checkpoints/<pack_hash>.json
"""

import hashlib
import json
import os
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
    stage: str                        # scan/download/apply/delete/verify/done
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
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(cp.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：断电也不会读到半截 JSON
        return True
    except Exception:  # noqa: BLE001
        return False


def _from_dict(data: dict, fallback_root: str = "") -> Checkpoint:
    return Checkpoint(
        pack_root=data.get("pack_root", fallback_root),
        stage=data.get("stage", ""),
        done=list(data.get("done", [])),
        remaining=list(data.get("remaining", [])),
        new_root=data.get("new_root", ""),
        updated_at=float(data.get("updated_at", 0.0) or 0.0),
        failed=list(data.get("failed", [])),
    )


def scan_checkpoints() -> list[Checkpoint]:
    """
    扫描全部未完成的检查点（启动时用：判断上次更新是否被中断）。

    stage 为 "done"/"applied" 的残留会被直接清掉（说明更新已经跑完，
    只是没来得及删）；只有真正中断在中间的才会返回。
    """
    d = _checkpoint_dir()
    if d is None:
        return []
    result: list[Checkpoint] = []
    try:
        for path in d.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            cp = _from_dict(data)
            if not cp.pack_root:
                continue
            if cp.stage in ("done", "applied"):
                clear_checkpoint(Path(cp.pack_root))
                continue
            result.append(cp)
    except OSError:
        pass
    result.sort(key=lambda c: c.updated_at, reverse=True)
    return result


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
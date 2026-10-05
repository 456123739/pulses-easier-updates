"""
trash.py — 回收站
------------------------------------------------
被删除的文件移入 <整合包同级>/.pulses_trash/<timestamp>/，
保留最近一次删除记录，供回滚与清理。
"""

import json
import shutil
import time
from pathlib import Path

_TRASH_DIRNAME = ".pulses_trash"
_DEFAULT_RETENTION_DAYS = 7


def _trash_root(pack_root: Path) -> Path:
    pack_root = Path(pack_root)
    return pack_root.parent / _TRASH_DIRNAME


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def move_to_trash(pack_root: Path, rel_paths: list[Path]) -> dict:
    """
    把整合包内指定的相对路径文件移动到回收站。
    返回 {'dir': trash_session_dir, 'moved': [...], 'failed': [...]}
    """
    pack_root = Path(pack_root)
    session = _trash_root(pack_root) / _timestamp()
    moved: list[dict] = []
    failed: list[dict] = []

    for rel in rel_paths:
        src = pack_root / rel
        if not src.exists():
            continue
        dst = session / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append({"rel": rel.as_posix(),
                          "trashed_to": str(dst)})
        except Exception as e:  # noqa: BLE001
            failed.append({"rel": rel.as_posix(), "error": str(e)})

    # 写 manifest
    try:
        session.mkdir(parents=True, exist_ok=True)
        (session / "manifest.json").write_text(
            json.dumps({"moved": moved, "failed": failed,
                        "created_at": time.time()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass

    return {"dir": session, "moved": moved, "failed": failed}


def restore_session(session_dir: Path) -> bool:
    """从某个回收站会话恢复（回滚用）"""
    session_dir = Path(session_dir)
    manifest = session_dir / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False

    ok = True
    for item in data.get("moved", []):
        dst = Path(item["trashed_to"])
        # 目标相对路径重新推算：以 trash_root 的父级为 pack_root
        pack_root = session_dir.parent.parent
        rel = dst.relative_to(session_dir)
        target = pack_root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(target))
        except Exception:  # noqa: BLE001
            ok = False
    return ok


def cleanup_trash(pack_root: Path,
                  retention_days: int = _DEFAULT_RETENTION_DAYS) -> int:
    """清理过期回收站会话，返回删除的会话数"""
    trash = _trash_root(pack_root)
    if not trash.is_dir():
        return 0
    threshold = time.time() - retention_days * 86400
    removed = 0
    try:
        for session in trash.iterdir():
            if not session.is_dir():
                continue
            try:
                if session.stat().st_mtime < threshold:
                    shutil.rmtree(session, ignore_errors=True)
                    removed += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return removed
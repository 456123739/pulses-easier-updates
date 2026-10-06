"""
trash.py — 回收站
-----------------------------------------------
被删除/被替换的文件与目录移入 <整合包同级>/.pulses_trash/<timestamp>/，
保留最近若干次操作的记录，供回滚与过期清理。

设计要点：
  - **一次更新 = 一个会话（session）**：同一次 execute_plan 产生的所有
    备份都落在同一个目录，manifest 只写一次并做合并，不会互相覆盖。
  - 会话目录名带序号去重，避免同一秒内的多次操作撞名。
  - manifest 记录 pack_root，恢复时不再靠目录层级反推（旧实现反推错了
    一层，restore 会恢复到整合包的上一级）。
  - 备份与恢复都用 shutil.move（同卷内是 rename，快且原子性更好）。
"""

import json
import os
import shutil
import time
from pathlib import Path

_TRASH_DIRNAME = ".pulses_trash"
_DEFAULT_RETENTION_DAYS = 7


def _trash_root(pack_root: Path) -> Path:
    pack_root = Path(pack_root)
    parent = pack_root.parent
    # 盘符根目录（C:\ / /）的 parent 是自己，仍然落在同一卷内
    return parent / _TRASH_DIRNAME


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def new_session(pack_root: Path) -> Path:
    """
    新建一个回收站会话目录并返回。

    同一个 pack_root 在同一秒内多次调用会依次得到 <ts>、<ts>-1、<ts>-2，
    不会互相覆盖。
    """
    root = _trash_root(pack_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return root / _timestamp()
    base = _timestamp()
    session = root / base
    n = 1
    while session.exists():
        session = root / f"{base}-{n}"
        n += 1
    try:
        session.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return session


def latest_session(pack_root: Path) -> Path | None:
    """返回最近一次回收站会话目录（按 mtime），没有则 None。"""
    root = _trash_root(pack_root)
    if not root.is_dir():
        return None
    best: Path | None = None
    best_m = -1.0
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                m = child.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best_m, best = m, child
    except OSError:
        return None
    return best


def _read_manifest(session: Path) -> dict:
    path = session / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _merge_manifest(session: Path, pack_root: Path,
                    moved: list[dict], failed: list[dict]) -> None:
    """写 manifest：同一会话内多次调用时**追加**而不是覆盖。"""
    data = _read_manifest(session)
    old_moved = data.get("moved", [])
    old_failed = data.get("failed", [])
    if not isinstance(old_moved, list):
        old_moved = []
    if not isinstance(old_failed, list):
        old_failed = []
    payload = {
        "pack_root": str(Path(pack_root)),
        "created_at": data.get("created_at", time.time()),
        "updated_at": time.time(),
        "moved": old_moved + moved,
        "failed": old_failed + failed,
    }
    try:
        session.mkdir(parents=True, exist_ok=True)
        tmp = session / "manifest.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, session / "manifest.json")
    except Exception:  # noqa: BLE001, S110
        pass


def move_to_trash(pack_root: Path, rel_paths: list[Path],
                  session: Path | None = None) -> dict:
    """
    把整合包内指定的相对路径（文件或目录）移动到回收站。

    session 传入时复用该会话（一次更新的所有备份放一起）；
    不传则新建。

    返回 {'dir': trash_session_dir, 'moved': [...], 'failed': [...]}
    """
    pack_root = Path(pack_root)
    if session is None:
        session = new_session(pack_root)
    session = Path(session)
    moved: list[dict] = []
    failed: list[dict] = []

    for rel in rel_paths:
        rel = Path(rel)
        src = pack_root / rel
        if not src.exists():
            continue
        dst = session / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                # 同一路径在本次会话里已经备份过（例如目录替换后又被删除）
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    dst.unlink()
            shutil.move(str(src), str(dst))
            moved.append({"rel": rel.as_posix(), "trashed_to": str(dst)})
        except Exception as e:  # noqa: BLE001
            failed.append({"rel": rel.as_posix(), "error": str(e)})

    _merge_manifest(session, pack_root, moved, failed)
    return {"dir": session, "moved": moved, "failed": failed}


def restore_entry(session_dir: Path, rel: Path) -> bool:
    """把会话里的单个条目恢复回整合包（回滚用）。"""
    session_dir = Path(session_dir)
    rel = Path(rel)
    src = session_dir / rel
    if not src.exists():
        return False
    data = _read_manifest(session_dir)
    pack_root_raw = data.get("pack_root", "")
    if not pack_root_raw:
        # 兼容旧 manifest：旧格式没有 pack_root 字段，退化为会话父目录的父目录
        pack_root = session_dir.parent.parent
    else:
        pack_root = Path(pack_root_raw)
    target = pack_root / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
        shutil.move(str(src), str(target))
        return True
    except Exception:  # noqa: BLE001
        return False


def restore_session(session_dir: Path) -> bool:
    """从某个回收站会话整批恢复（回滚用）。"""
    session_dir = Path(session_dir)
    data = _read_manifest(session_dir)
    if not data:
        return False
    ok = True
    for item in data.get("moved", []):
        rel = item.get("rel")
        if not rel:
            continue
        if not restore_entry(session_dir, Path(rel)):
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


def cleanup_all_trash(retention_days: int = _DEFAULT_RETENTION_DAYS
                      ) -> int:
    """
    清理**已定位整合包**的过期回收站。

    回收站目录位于整合包同级目录，无法全局枚举；这里清理最近打开过的
    整合包（由 recent 列表提供），启动时调用即可防止只增不减。
    """
    total = 0
    try:
        from . import recent
        for path in recent.load_recent():
            try:
                total += cleanup_trash(Path(path), retention_days)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return total
    return total

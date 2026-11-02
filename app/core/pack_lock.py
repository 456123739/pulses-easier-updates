"""
pack_lock.py — 更新期间的"整合包占用锁" + 变更守卫
------------------------------------------------
两件事，别混：

1) **锁**：同一时间只允许一个 Easier 实例对同一个整合包做扫描/更新。
   实现是一个放在用户数据目录里的锁文件 + 操作系统的**文件锁**
   （Windows 用 `msvcrt.locking`，POSIX 用 `fcntl.flock`）。
   - 内核在进程退出时自动释放（崩溃/被杀也一样），不需要"清理残留"；
   - 不做 ACL、不改权限、不锁玩家文件，开销只有一次 open + 一次 lock；
   - 位置在 `%LOCALAPPDATA%/locks/<hash>.lock`，不往整合包里塞东西。

2) **变更守卫**：扫描时记一份 `{相对路径: (mtime_ns, size)}`，应用前再比一次。
   如果有人在这期间改了整合包（比如另开启动器、手动拖文件），直接拒绝更新并
   提示重新扫描——比"锁死文件夹"现实得多：Windows 上想禁止所有外部写入只能改
   ACL（要管理员、崩溃会留下拒绝写入的 ACL），得不偿失。

两者都是 O(文件数) 的 stat，不读文件内容，不占 CPU。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

LOCK_NAME = "pulses-easier"
# 这些目录/文件名在比对变更时忽略：游戏自己在写、或者是我们的临时文件
IGNORED_TOPS = frozenset({"logs", "crash-reports"})
IGNORED_NAMES = frozenset({"desktop.ini", ".DS_Store", "Thumbs.db"})
IGNORED_PREFIX = (".pulses_tmp", ".pulses-easier")


class PackBusy(RuntimeError):
    """同一个整合包已经有别的窗口在扫描/更新。"""

    code = "PACK_BUSY"


class PackChanged(RuntimeError):
    """扫描之后整合包被改动过。"""

    code = "PACK_CHANGED"


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if not base:
        base = str(Path.home() / ".local" / "share")
    path = Path(base) / "Pulses Easier" / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_path_for(root: str | Path) -> Path:
    try:
        real = os.path.realpath(root)
    except OSError:
        real = str(root)
    key = hashlib.sha1(real.lower().encode("utf-8", "replace")).hexdigest()[:16]
    return _data_dir() / f"{LOCK_NAME}-{key}.lock"


class PackLock:
    """进程级排他锁；进程退出（含崩溃）时由内核释放。"""

    def __init__(self, root: str | Path, action: str = "更新") -> None:
        self.root = Path(root)
        self.action = action
        self.path = lock_path_for(self.root)
        self.meta_path = self.path.with_suffix(".json")
        self._fh = None

    # ---- 生命周期 ---------------------------------------------------
    def acquire(self) -> "PackLock":
        fh = open(self.path, "a+b")
        try:
            # Windows 的 msvcrt.locking 锁的是字节范围，空文件也能锁，
            # 但先补一个字节更稳（各家实现对这个边界不一致）。
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            _lock_file(fh)
        except OSError:
            owner = _read_owner(self.meta_path)
            fh.close()
            raise PackBusy(
                f"这个整合包正在被另一个窗口{owner or '使用'}", ) from None
        self._fh = fh
        # 元数据写在**另一个**文件里：锁住的字节范围在不同进程/句柄下
        # 连读都会被拒绝（Windows 的字节锁就是这么工作的）。
        try:
            self.meta_path.write_text(json.dumps({
                "pid": os.getpid(),
                "action": self.action,
                "root": str(self.root),
                "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock_file(fh)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        try:
            self.meta_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "PackLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _lock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_owner(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return ""
    action = str(data.get("action") or "")
    since = str(data.get("since") or "")
    if not action:
        return ""
    return f"{'正在' + action if action else ''}{('（' + since + ' 开始）') if since else ''}"


# ----------------------------------------------------------------------
# 变更守卫
# ----------------------------------------------------------------------
def _skip(rel: str) -> bool:
    parts = rel.split("/")
    if parts and parts[0].lower() in IGNORED_TOPS:
        return True
    name = parts[-1]
    if name in IGNORED_NAMES:
        return True
    return any(p.startswith(IGNORED_PREFIX) for p in parts)


def snapshot(root: str | Path) -> dict[str, tuple[int, int]]:
    """`{相对路径: (mtime_ns, size)}`，忽略日志与我们自己的临时文件。"""
    root = Path(root)
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir == "":
            dirnames[:] = [d for d in dirnames if d.lower() not in IGNORED_TOPS]
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _skip(rel):
                continue
            try:
                st = (here / name).stat()
            except OSError:
                continue
            out[rel] = (st.st_mtime_ns, st.st_size)
    return out


def changes(old: dict[str, tuple[int, int]],
            new: dict[str, tuple[int, int]]) -> list[str]:
    """扫描之后被改动/新增/删除的文件（相对路径，最多返回 50 条）。"""
    diff: list[str] = []
    for rel, meta in new.items():
        if old.get(rel) != meta:
            diff.append(rel)
            if len(diff) >= 50:
                return diff
    for rel in old:
        if rel not in new:
            diff.append(rel)
            if len(diff) >= 50:
                return diff
    return diff


def guard(old: dict[str, tuple[int, int]], root: str | Path) -> list[str]:
    return changes(old, snapshot(root))


def describe(changed: list[str]) -> str:
    head = "、".join(changed[:3])
    more = f" 等 {len(changed)} 个文件" if len(changed) > 3 else ""
    return f"{head}{more}"

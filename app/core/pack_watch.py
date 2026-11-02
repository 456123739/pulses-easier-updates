"""
pack_watch.py — 更新途中的「外部改动」看门狗
------------------------------------------------
只盯**这次更新即将改写的目标**：计划里要覆盖/删除的文件、要整块替换的目录。
Easier 自己动过的目标由 `mark_done` 移出监视，所以"自己改的"不会误报。

判定用 stat（mtime + 大小），一次轮询只读元数据：
- 普通文件目标：一次 stat；
- 整目录目标：整棵子树快照（能同时发现"改了"和"多了一个新文件"）。
"""

from __future__ import annotations

from pathlib import Path

from . import pack_lock


def _state(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class PackWatch:
    """监视一组"即将被改写"的目标。"""

    def __init__(self, root: str | Path, files=(), dirs=()) -> None:
        self.root = Path(root)
        self._files: dict[str, tuple[int, int] | None] = {}
        self._dirs: dict[str, dict[str, tuple[int, int]]] = {}
        for rel in files:
            rel = str(rel).replace("\\", "/")
            if pack_lock._skip(rel):
                continue
            self._files[rel] = _state(self.root / rel)
        for rel, _children in dirs:
            rel = str(rel).replace("\\", "/")
            self._dirs[rel + "/"] = pack_lock.snapshot(self.root / rel)

    def __bool__(self) -> bool:
        return bool(self._files or self._dirs)

    def pending(self) -> list[str]:
        return sorted(self._files) + sorted(self._dirs)

    def mark_done(self, rel: str) -> None:
        """Easier 刚处理完这个目标（整目录会连同子文件一起移出）。"""
        key = str(rel).replace("\\", "/")
        self._files.pop(key, None)
        if key in self._dirs:
            self._dirs.pop(key, None)
            return
        prefix = key if key.endswith("/") else key + "/"
        for unit in [u for u in self._dirs if u == prefix or u.startswith(prefix)]:
            self._dirs.pop(unit, None)

    def poll(self) -> list[str]:
        """外部改动过的具体文件（整目录则展开成目录下的文件）。"""
        changed: list[str] = []
        for rel, base in self._files.items():
            if _state(self.root / rel) != base:
                changed.append(rel)
        for unit, base in self._dirs.items():
            now = pack_lock.snapshot(self.root / unit.rstrip("/"))
            for rel, state in now.items():
                if base.get(rel) != state:
                    changed.append(unit + rel)
            for rel in base:
                if rel not in now:
                    changed.append(unit + rel)
        return sorted(set(changed))

    def rebase(self, rels=None) -> None:
        """玩家选择「继续」：把当前状态当成新基线。"""
        wanted = set(rels) if rels else None
        for rel in list(self._files):
            if wanted is None or rel in wanted:
                self._files[rel] = _state(self.root / rel)
        for unit, base in self._dirs.items():
            if wanted is not None and not any(r.startswith(unit) for r in wanted):
                continue
            now = pack_lock.snapshot(self.root / unit.rstrip("/"))
            for rel in set(base) | set(now):
                base[rel] = now.get(rel, base.get(rel))   # type: ignore[assignment]


def watch_for_plan(root: str | Path, plan) -> PackWatch:
    """按更新计划建看门狗（只含真正会被改写的目标）。"""
    files = [p.as_posix() for p in plan.copy] + \
            [p.as_posix() for p in plan.delete]
    dirs = [(rel.as_posix(), plan.dir_files.get(rel.as_posix(), []))
            for rel, _strat in plan.replace_dirs]
    return PackWatch(root, files, dirs)


def describe(changed: list[str], limit: int = 3) -> str:
    if not changed:
        return ""
    head = "、".join(Path(c.rstrip("/")).name for c in changed[:limit])
    more = f" 等 {len(changed)} 个文件" if len(changed) > limit else ""
    return head + more

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
    stage: str                        # scan/download/apply/copy/delete/verify
    done: list[str] = field(default_factory=list)   # 最近若干条（游标见下）
    remaining: list[str] = field(default_factory=list)
    new_root: str = ""
    updated_at: float = 0.0
    failed: list[dict] = field(default_factory=list)
    done_count: int = 0               # 累计完成条数（游标，不随列表截断丢失）

    def to_dict(self) -> dict:
        return asdict(self)


def checkpoint_path(pack_root) -> Path | None:
    """检查点文件路径（顺带把目录建好）。"""
    d = _checkpoint_dir()
    if d is None:
        return None
    return d / f"{_pack_hash(Path(pack_root))}.json"


def _write_cp(path: Path, cp: Checkpoint) -> bool:
    cp.updated_at = time.time()
    if len(cp.done) > _KEEP_DONE:
        cp.done = cp.done[-_KEEP_DONE:]
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(cp.to_dict(), ensure_ascii=False,
                       separators=(",", ":")),
            encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：断电也不会读到半截 JSON
        return True
    except Exception:  # noqa: BLE001
        return False


def save_checkpoint(cp: Checkpoint) -> bool:
    """写检查点。

    历史问题：`done` 是整个更新过程的**全量**完成列表，每 25 个文件重写
    一次带缩进的 JSON —— 2 万个文件时要写出上百 MB，实测占应用阶段
    74% 的耗时（O(n²)）。现在：
      - `done` 只保留最近 `_KEEP_DONE` 条（人看/调试用）
      - 累计数量记在 `done_count`
      - JSON 去掉缩进
    调用方也不会再每个文件都写一次（见 CheckpointWriter）。
    """
    path = checkpoint_path(cp.pack_root)
    if path is None:
        return False
    return _write_cp(path, cp)


def _from_dict(data: dict, fallback_root: str = "") -> Checkpoint:
    done = list(data.get("done", []))
    return Checkpoint(
        pack_root=data.get("pack_root", fallback_root),
        stage=data.get("stage", ""),
        done=done,
        remaining=list(data.get("remaining", [])),
        new_root=data.get("new_root", ""),
        updated_at=float(data.get("updated_at", 0.0) or 0.0),
        failed=list(data.get("failed", [])),
        done_count=int(data.get("done_count", len(done)) or 0),
    )


# done 列表最多保留这么多条（只影响人看的信息量，不影响"是否中断"的判断）
_KEEP_DONE = 200


class CheckpointWriter:
    """
    检查点写入器：**按时间节流**，而不是每 N 个文件写一次。

    更新过程只需要"能判断上次是不是正常结束"，不需要逐文件流水账。
    默认每 0.5 秒最多落盘一次，且阶段切换时立即落盘。
    """

    def __init__(self, pack_root, stage: str = "apply",
                 new_root: str = "", min_interval: float = 1.0,
                 every: int = 5000):
        self.cp = Checkpoint(
            pack_root=str(pack_root), stage=stage, done=[],
            remaining=[], new_root=str(new_root or ""))
        self.min_interval = max(0.05, float(min_interval))
        self.every = max(1, int(every))
        self._last = 0.0
        self._pending = 0
        # 路径只解析一次：get_db_path() 每次都要读一遍 config.json
        self._path = checkpoint_path(pack_root)
        self.save(force=True)

    def save(self, force: bool = False) -> bool:
        now = time.time()
        if not force and self._pending < self.every \
                and now - self._last < self.min_interval:
            return False
        self._last = now
        self._pending = 0
        if self._path is None:
            return False
        return _write_cp(self._path, self.cp)

    def stage(self, name: str, remaining: list[str] | None = None) -> None:
        self.cp.stage = name
        if remaining is not None:
            self.cp.remaining = list(remaining)
        self.save(force=True)

    def done(self, key: str) -> None:
        self.cp.done.append(key)
        self.cp.done_count += 1
        self._pending += 1
        self.save()

    def mark_failed(self, item: dict) -> None:
        self.cp.failed.append(item)

    def finish(self, stage: str = "verify") -> None:
        """收尾：清掉 remaining，落到指定阶段（默认 verify）。"""
        self.cp.stage = stage
        self.cp.remaining = []
        self.save(force=True)


def mark_applied(pack_root) -> bool:
    """
    把检查点标成"校验也通过了"。

    只有调用方（应用完成后还跑了一遍 verify_after_update）才能这么说，
    所以由 `updater.execute_plan` 之外的地方调用。
    """
    cp = load_checkpoint(Path(pack_root))
    if cp is None:
        cp = Checkpoint(pack_root=str(pack_root), stage="applied", done=[],
                        remaining=[], new_root="")
    cp.stage = "applied"
    cp.remaining = []
    return save_checkpoint(cp)


def load_checkpoint(pack_root) -> Checkpoint | None:
    """读某个整合包的检查点（不存在/坏了返回 None）。"""
    path = checkpoint_path(pack_root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return _from_dict(data, str(pack_root))


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
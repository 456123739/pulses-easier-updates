"""
updater.py — 按策略执行更新
------------------------------------------------
plan 生成规则：
  白名单文件夹（文件级变更）：
    - ADDED 文件 → copy
    - MODIFIED 文件 → 完全匹配/替换重名 copy；跳过重名 skip
    - DELETED 文件 → 完全匹配 delete；其他 skip
  非白名单文件夹（文件夹级变更）：
    - ADDED 目录 → 整个目录 copy
    - MODIFIED 目录 → 按策略加入 replace_dirs：
        · 完全匹配 → 删旧目录 + 整目录复制（走 replace_dir 完全替换）
        · 替换重名 → 目录合并（同名覆盖）
        · 跳过重名 → 目录合并（同名跳过）
    - DELETED 目录 → 完全匹配 delete 整个目录；其他 skip

执行阶段：
  1) replace_dirs 目录操作（事务性：旧目录先改名，任一步失败整体回滚）
  2) copy 文件（逐个原子替换：临时名 → os.replace）
  3) delete 文件/目录（永久删除）

搬运统一走 `transfer.move_in`（见 transfer.py）：
  - 同卷：rename / 硬链接，**元数据操作，不产生读写**，也不会"一次复制一大坨"
  - 跨卷：分块复制 + 原子替换，块间可节流；批量之间按字节数批次让出
  - 下载缓存保留（方便重试/续传），解压出来的 overrides 搬运后消耗
"""

import hashlib
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChangeKind, Strategy
from ..utils.files import safe_join
from . import checkpoint as cp_mod
from . import transfer
from .apply_rules import (
    is_checked_n,
    normalize_keys,
    resolve_strategy_n,
)
from .transfer import TransferResult

# 批量搬运的默认节流参数：每搬够这么多字节，暂停一下让出磁盘/CPU。
# 对同卷"移动"没有任何影响（本来就不产生 IO）。
BATCH_BYTES = 96 * 1024 * 1024
BATCH_PAUSE_MS = 12.0


class InsufficientSpace(RuntimeError):
    """目标磁盘放不下这次更新（开始前就拒绝，绝不留半截）。"""


@dataclass
class UpdatePlan:
    # 文件级操作
    copy: list[Path] = field(default_factory=list)
    delete: list[Path] = field(default_factory=list)
    skip: list[Path] = field(default_factory=list)
    # 目录级操作：(相对路径, 策略)
    replace_dirs: list[tuple[Path, Strategy]] = field(default_factory=list)
    # ---- 期望值（生成计划时按来源统计，用于搬运后校验） ----
    # rel → 应当就位的大小
    expect_size: dict[str, int] = field(default_factory=dict)
    # rel → "sha1:..."（下载源带摘要时才有），跨卷复制时顺带校验
    expect_hash: dict[str, str] = field(default_factory=dict)
    # 目录 rel → 该目录下来源里的文件清单
    dir_files: dict[str, list[str]] = field(default_factory=dict)
    # 目录 rel → 该目录下"允许保留玩家版本"的文件（跳过重名策略）
    dir_optional: dict[str, list[str]] = field(default_factory=dict)


# ----------------------------------------------------------------------
# 来源：按相对路径取"新内容"
# ----------------------------------------------------------------------
@dataclass
class SourceLayer:
    """一层来源。优先级按传入顺序，先找到的先用。"""

    root: Path
    allowed: set[str] | None = None      # None = 整棵树都算
    consume: bool = True                 # 搬运成功后是否删掉源文件
    ignore: Callable[[Path], bool] | None = None

    def __post_init__(self):
        self.root = Path(self.root)
        self._root_s = str(self.root)

    def usable_key(self, key: str) -> bool:
        """key 是包根相对路径的 posix 字符串。"""
        if self.allowed is not None and key not in self.allowed:
            return False
        if self.ignore is not None:
            try:
                if self.ignore(Path(key)):
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def usable(self, rel: Path) -> bool:
        return self.usable_key(rel.as_posix())


class PlanSource:
    """
    计划的"内容来源"，替代了旧实现里"先把整包复制成 _merged 副本"的做法。

    旧做法要先把 overrides + 下载缓存全量复制一份到临时目录（1GB 内容要
    多写 1GB，且在 UI 线程上做），既慢又占空间。现在改成按需取：
    plan 里需要哪个文件，就去来源里找哪一个。
    """

    def __init__(self, layers: list[SourceLayer]):
        self.layers = [ly for ly in layers if ly and ly.root]

    # -- 查找 ----------------------------------------------------------
    def find(self, rel: Path) -> tuple[Path, SourceLayer] | None:
        # 这里是每个文件都要走的路径，用字符串拼路径比 Path 运算符快很多
        key = rel.as_posix()
        for ly in self.layers:
            if not ly.usable_key(key):
                continue
            p = os.path.join(ly._root_s, key)
            try:
                if os.path.isfile(p):
                    return Path(p), ly
            except OSError:
                continue
        return None

    def path_for(self, rel: Path) -> Path | None:
        got = self.find(rel)
        return got[0] if got else None

    def size_for(self, rel: Path) -> int:
        got = self.find(rel)
        if got is None:
            return 0
        try:
            return got[0].stat().st_size
        except OSError:
            return 0

    # -- 目录 ----------------------------------------------------------
    def walk_dir(self, rel_dir: Path) -> tuple[list[Path], list[Path]]:
        """
        列出源里某个目录下的全部内容（相对包根的 rel）。

        返回 (文件, 目录)；同名以**先出现的层**为准（缓存优先于 overrides）。
        """
        files: list[Path] = []
        dirs: list[Path] = []
        seen: set[str] = set()
        sub = rel_dir.as_posix()
        for ly in self.layers:
            base = ly.root / rel_dir
            try:
                if not base.is_dir():
                    continue
            except OSError:
                continue
            base_s = ly._root_s
            cut = len(base_s) + 1
            # followlinks=True：与旧的 rglob 行为一致（软链接目录也要复制）
            for root, dnames, fnames in os.walk(
                    transfer.sys_path(base), followlinks=True):
                for name in dnames:
                    key = os.path.join(root, name)[cut:].replace(os.sep, "/")
                    if key in seen or not ly.usable_key(key):
                        continue
                    seen.add(key)
                    dirs.append(Path(key))
                for name in fnames:
                    key = os.path.join(root, name)[cut:].replace(os.sep, "/")
                    if key in seen or not ly.usable_key(key):
                        continue
                    seen.add(key)
                    files.append(Path(key))
        return files, dirs

    def has_dir(self, rel_dir: Path) -> bool:
        for ly in self.layers:
            try:
                if (ly.root / rel_dir).is_dir():
                    return True
            except OSError:
                continue
        return False

    # -- 搬运 ----------------------------------------------------------
    def move_pair(self, pair, dst: Path, *, expect_size: int = 0,
                  expect_hash: str = "",
                  chunk_bytes: int = transfer.CHUNK_BYTES,
                  pace_ms: float = 0.0, ensure_parent: bool = True,
                  should_abort=None) -> TransferResult:
        src, layer = pair
        algo, _, hexd = (expect_hash or "").partition(":")
        if not hexd:
            algo, hexd = "sha1", ""
        # 跨卷复制时优先交给系统的快速复制（Windows CopyFile2 / Linux
        # sendfile）；只有需要逐块校验或节流时才用 Python 分块循环。
        no_fast = bool(hexd) or pace_ms > 0
        return transfer.move_in(
            src, dst, preserve=not layer.consume,
            chunk_bytes=chunk_bytes, pace_ms=pace_ms,
            expect_size=expect_size, expect_hash=hexd,
            algo=algo or "sha1", ensure_parent=ensure_parent,
            no_fast_copy=no_fast, should_abort=should_abort)

    def take(self, rel: Path, dst: Path, *, expect_size: int = 0,
             expect_hash: str = "", chunk_bytes: int = transfer.CHUNK_BYTES,
             pace_ms: float = 0.0, ensure_parent: bool = True,
             should_abort=None) -> TransferResult:
        pair = self.find(rel)
        if pair is None:
            return TransferResult(False, "来源里找不到这个文件",
                                  missing=True)
        return self.move_pair(
            pair, dst, expect_size=expect_size, expect_hash=expect_hash,
            chunk_bytes=chunk_bytes, pace_ms=pace_ms,
            ensure_parent=ensure_parent, should_abort=should_abort)


def _is_added(rel: Path, diff) -> bool:
    """这条目录级变更是不是"新增目录"。"""
    for ch in getattr(diff, "added", ()) or ():
        if getattr(ch, "rel_path", None) == rel:
            return True
    return False


def fill_expectations(plan: UpdatePlan, source: PlanSource,
                      hash_for: Callable[[str], str] | None = None) -> None:
    """
    按来源登记"应用后应当就位的大小/摘要"。

    这是"搬运"能成立的前提：文件搬过去之后源可能已经不在了（同卷移动），
    校验不能再回头去读源，只能靠事先记下的期望值。
    """
    targets = list(plan.copy)
    for files in plan.dir_files.values():
        targets.extend(Path(f) for f in files)
    for rel in targets:
        key = rel.as_posix()
        if key in plan.expect_size:
            continue
        got = source.find(rel)
        if got is None:
            continue
        try:
            plan.expect_size[key] = got[0].stat().st_size
        except OSError:
            continue
    if hash_for is not None:
        for key in plan.expect_size:
            if key in plan.expect_hash:
                continue
            value = hash_for(key)
            if value:
                plan.expect_hash[key] = value


def build_plan(diff, checked, strategies,
               old_root: Path | None = None,
               new_root: Path | None = None,
               source: PlanSource | None = None,
               hash_for: Callable[[str], str] | None = None) -> UpdatePlan:
    """
    生成执行计划。判定规则统一来自 apply_rules（与界面灰显、
    下载任务收集共用同一套逻辑）。

    传入 `source` 时顺带统计"每个文件应当多大/什么摘要"，应用完成后
    就不再需要源文件还在原位也能校验（这是"搬运"能成立的前提）。
    """
    plan = UpdatePlan()
    if source is None and new_root is not None:
        # 兼容老调用：把单个目录当成一层"不消耗"的来源
        source = PlanSource([SourceLayer(Path(new_root), consume=False)])

    # 归一化一次（ContentType 键 → 目录名），循环里不再逐条转换
    checked_n = normalize_keys(checked)
    strategies_n = normalize_keys(strategies)

    for change in diff.added + diff.modified + diff.deleted:
        rel = change.rel_path
        parts = rel.parts
        top = parts[0] if parts else ""

        if not is_checked_n(checked_n, top):
            plan.skip.append(rel)
            continue

        strat = resolve_strategy_n(strategies_n, top)

        # ---- 文件夹级（非白名单） ----
        if change.is_folder_level:
            if change.kind == ChangeKind.ADDED:
                # 整个目录新增：加入 copy（执行时走目录复制）
                plan.replace_dirs.append((rel, Strategy.FULL_MATCH))
            elif change.kind == ChangeKind.MODIFIED:
                plan.replace_dirs.append((rel, strat))
            elif change.kind == ChangeKind.DELETED:
                if strat == Strategy.FULL_MATCH:
                    # 整目录删除：直接交给 rmtree 一次搞定。
                    # 旧实现是把目录展开成上万个文件条目逐个 unlink
                    # （实测 2 万个文件 2.3s vs rmtree 0.3s）。
                    plan.delete.append(rel)
                else:
                    plan.skip.append(rel)
            continue

        # ---- 文件级（白名单） ----
        if change.kind == ChangeKind.ADDED:
            plan.copy.append(rel)
        elif change.kind == ChangeKind.MODIFIED:
            if strat == Strategy.SKIP_SAME:
                plan.skip.append(rel)
            else:
                plan.copy.append(rel)
        elif change.kind == ChangeKind.DELETED:
            if strat == Strategy.FULL_MATCH:
                plan.delete.append(rel)
            else:
                plan.skip.append(rel)

    # ---- 统一登记期望值（目录级 + 文件级） ----
    if source is not None:
        kept_dirs: list[tuple[Path, Strategy]] = []
        for rel, strat in plan.replace_dirs:
            files, _dirs = source.walk_dir(rel)
            key = rel.as_posix()
            # 新增目录 + 来源里根本没有 → 这一项无事可做，不算失败
            # （索引里有、但既没下载也没随包附带的情况）
            if not files and not source.has_dir(rel) \
                    and _is_added(rel, diff):
                continue
            plan.dir_files[key] = [f.as_posix() for f in files]
            if strat == Strategy.SKIP_SAME and old_root is not None:
                plan.dir_optional[key] = [
                    f.as_posix() for f in files
                    if safe_join(Path(old_root), f).exists()]
            kept_dirs.append((rel, strat))
        plan.replace_dirs = kept_dirs
        fill_expectations(plan, source, hash_for)

    return plan


# ----------------------------------------------------------------------
# 目录级操作（事务性回滚：失败不留半个目录）
# ----------------------------------------------------------------------
def _unique_sidecar(dst: Path) -> Path:
    """
    同目录下的临时名（同卷 rename 很快，且不会跨盘失败）。

    用点号开头 + 固定后缀，尽量不干扰用户；正常流程结束后它不会留下。
    """
    base = "." + dst.name + ".pulses_tmp"
    cand = dst.with_name(base)
    n = 1
    while cand.exists():
        cand = dst.with_name(f"{base}{n}")
        n += 1
    return cand


def _rmtree_quiet(p: Path):
    try:
        shutil.rmtree(transfer.sys_path(p), ignore_errors=True)
    except Exception:  # noqa: BLE001, S110
        pass


def _replace_dir(rel: Path, strat: Strategy, old_root: Path,
                 source: PlanSource, log: Callable[[str, str], None],
                 *, chunk_bytes: int, pace_ms: float,
                 should_abort=None) -> tuple[bool, TransferResult]:
    """
    目录级操作。返回 (是否成功, 最后一次搬运结果)。

    完全匹配：旧目录先 rename 到同目录临时名 → 逐个搬运新内容 →
              全部成功才删临时名；中途失败则删掉半个新目录并 rename 回来。
    替换重名 / 跳过重名：目录合并，只写不删（玩家自己的文件不受影响）。
    """
    dst_root = old_root / rel
    files, dirs = source.walk_dir(rel)
    full = strat == Strategy.FULL_MATCH
    backup: Path | None = None

    if full and (dst_root.exists() or dst_root.is_symlink()):
        try:
            backup = _unique_sidecar(dst_root)
            os.replace(transfer.sys_path(dst_root), transfer.sys_path(backup))
        except Exception as e:  # noqa: BLE001
            log("error", f"替换目录失败 {rel.as_posix()}："
                         f"{transfer.friendly_os_error(e, '无法备份旧目录')}")
            return False, TransferResult(False, "无法备份旧目录")

    last = TransferResult(True)
    try:
        transfer.mkdirs(dst_root)
        for d in dirs:
            transfer.mkdirs(old_root / d)
        for f in files:
            dst = old_root / f
            if strat == Strategy.SKIP_SAME and dst.exists():
                continue                      # 跳过重名：保留玩家版本
            last = source.take(
                f, dst,
                expect_size=0,
                chunk_bytes=chunk_bytes, pace_ms=pace_ms,
                ensure_parent=False, should_abort=should_abort)
            if not last.ok:
                raise RuntimeError(last.error or "写入失败")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if full:
            _rmtree_quiet(dst_root)
            if backup is not None:
                try:
                    os.replace(transfer.sys_path(backup),
                               transfer.sys_path(dst_root))
                    log("warn", f"替换目录 {rel.as_posix()} 已回滚到替换前的内容")
                except Exception as e2:  # noqa: BLE001
                    log("error", f"{rel.as_posix()} 自动回滚失败："
                                 f"{transfer.friendly_os_error(e2)}；"
                                 f"原内容保留在 {backup}")
        log("error", f"目录操作失败 {rel.as_posix()}：{msg}")
        return False, (last if not last.ok else TransferResult(False, msg))

    if backup is not None:
        _rmtree_quiet(backup)
    if full:
        log("info", f"替换目录 {rel.as_posix()}")
    else:
        log("info", f"合并目录 {rel.as_posix()}"
                    f"（{'同名覆盖' if strat == Strategy.REPLACE_SAME else '同名跳过'}）")
    return True, last


def estimate_copy_bytes(plan: UpdatePlan, old_root: Path,
                        source: PlanSource) -> int:
    """
    估算这次应用**真正需要额外磁盘空间**的字节数。

    同卷搬运是 rename/硬链接（只改目录项，不占新空间），跨卷才真的写
    新数据；整目录替换期间旧目录会先改名留在原地，也会占着空间，
    所以对跨卷部分按"新内容大小"估算。
    """
    try:
        target_dev = os.stat(
            transfer.sys_path(transfer._existing_ancestor(old_root))).st_dev
    except OSError:
        return 0
    rels = list(plan.copy)
    for files in plan.dir_files.values():
        rels.extend(Path(f) for f in files)
    total = 0
    for rel in rels:
        pair = source.find(rel)
        if pair is None:
            continue
        src = pair[0]
        try:
            if os.stat(transfer.sys_path(src)).st_dev == target_dev:
                continue
            total += src.stat().st_size
        except OSError:
            continue
    return total


# ----------------------------------------------------------------------
# 失败重试（按用户要求：写入失败自动重试 1 次，仍失败才算失败）
# ----------------------------------------------------------------------
def _retry_once(fn: Callable[[], bool], log: Callable[[str, str], None],
                what: str, attempts: int = 2) -> bool:
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            if fn():
                return True
        except Exception as e:  # noqa: BLE001
            last_exc = e
        if i + 1 < attempts:
            log("warn", f"{what} 失败，正在重试一次…")
    if last_exc is not None:
        log("error", f"{what} 重试后仍失败：{last_exc}")
    return False


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def execute_plan(
    plan: UpdatePlan,
    old_root: Path,
    new_root: Path | None = None,
    *,
    source: PlanSource | None = None,
    log: Callable[[str, str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    chunk_bytes: int = transfer.CHUNK_BYTES,
    pace_ms: float = 0.0,
    batch_bytes: int = BATCH_BYTES,
    batch_pause_ms: float = BATCH_PAUSE_MS,
    space_check: bool = True,
    should_abort=None,
) -> dict:
    """
    按计划执行更新。

    搬运走 transfer.move_in：同卷是元数据操作（瞬时、无 IO），跨卷分块复制；
    目标端逐个原子替换，不存在"写了一半的整合包文件"。

    删除是**永久删除**（v0.5.0 起不再有回收站）：策略「完全匹配」的语义
    本身就是"含删除"，是否删除由用户在选择策略时决定。
    唯一会做备份的是"目录整体替换"，且只在失败时用于就地回滚。
    """
    old_root = Path(old_root)
    if source is None:
        root = Path(new_root) if new_root is not None else None
        source = PlanSource([SourceLayer(root, consume=False)]) if root \
            else PlanSource([])

    def _log(level: str, msg: str):
        if log:
            log(level, msg)

    def _prog(done: int, total: int):
        if progress:
            progress(done, total)

    report: dict = {
        "applied": 0,
        "deleted": 0,
        "skipped": len(plan.skip),
        "failed": [],
        "moved": 0,          # 瞬时搬运（rename/硬链接）的文件数
        "bytes": 0,          # 真正复制过的字节数
    }

    copy_list = list(dict.fromkeys(plan.copy))
    delete_list = list(dict.fromkeys(plan.delete))
    replace_list = list(dict.fromkeys(plan.replace_dirs))

    # 进度按"文件数"加权：整目录替换不再只算 1 步
    # （旧实现里换一个 2 万文件的目录和写一个小文件都是 1 步，进度条会跳）
    weights: dict[str, int] = {}
    for rel, _s in replace_list:
        weights[rel.as_posix()] = max(
            1, len(plan.dir_files.get(rel.as_posix(), [])))
    total_steps = (len(copy_list) + len(delete_list)
                   + sum(weights.values()))
    step = 0

    # 开始时先确认放得下：写一半才发现没空间，比一开始就拒绝糟糕得多
    if space_check:
        need = estimate_copy_bytes(plan, old_root, source)
        ok, msg = transfer.have_space_for(need, old_root)
        if not ok:
            _log("error", msg + "（本次更新尚未改动任何文件）")
            raise InsufficientSpace(msg)

    cpw = cp_mod.CheckpointWriter(
        old_root, stage="apply",
        new_root=str(new_root or ""))
    cpw.cp.remaining = [p.as_posix() for p, _ in replace_list]
    cpw.save(force=True)

    # 阶段 0：目录级替换（先做，保证整块被替换）
    for rel, strat in replace_list:
        if not source.has_dir(rel):
            # 计划里有、来源里没有：绝不能悄悄什么都不做
            _log("error", f"更新源里找不到目录 {rel.as_posix()}")
            report["failed"].append(
                {"rel": rel.as_posix(), "error": "更新源里找不到这个目录"})
            step += 1
            _prog(step, total_steps)
            continue

        def _one(r=rel, s=strat) -> bool:
            ok, res = _replace_dir(r, s, old_root, source, _log,
                                   chunk_bytes=chunk_bytes, pace_ms=pace_ms,
                                   should_abort=should_abort)
            if ok:
                if res.moved:
                    report["moved"] += 1
                report["bytes"] += res.bytes_copied
            return ok

        if _retry_once(_one, _log, f"替换目录 {rel.as_posix()}"):
            report["applied"] += 1
        else:
            report["failed"].append(
                {"rel": rel.as_posix(), "error": "目录操作失败"})
        step += weights.get(rel.as_posix(), 1)
        _prog(step, total_steps)

    # 阶段 1：文件级新增 + 替换（原子搬运 + 按字节分批让出）
    cpw.stage("copy")
    batch = 0
    ready_dirs: set[str] = {old_root.as_posix()}
    for rel in copy_list:
        key = rel.as_posix()
        expect_size = int(plan.expect_size.get(key, 0) or 0)
        expect_hash = plan.expect_hash.get(key, "")

        # 来源里确实没有这个文件（索引里有、但既没下载也没随包附带）：
        # 保持历史行为——跳过，交给应用后校验去报告"缺失"。
        pair = source.find(rel)
        if pair is None:
            _log("warn", f"跳过 {key}：更新源里没有这个文件")
            report["skipped"] += 1
            step += 1
            _prog(step, total_steps)
            continue

        # 父目录只建一次（同一个 mods/ 下几千个文件时，这一条最关键）
        dst = old_root / rel
        parent = dst.parent
        parent_key = parent.as_posix()
        need_parent = parent_key not in ready_dirs

        def _copy(pr=pair, d=dst, k=key, es=expect_size, eh=expect_hash,
                  p=parent, pk=parent_key, mk=need_parent) -> bool:
            if mk and pk not in ready_dirs:
                transfer.mkdirs(p)
                ready_dirs.add(pk)
            res = source.move_pair(
                pr, d, expect_size=es, expect_hash=eh,
                chunk_bytes=chunk_bytes, pace_ms=pace_ms,
                ensure_parent=False, should_abort=should_abort)
            if res.ok:
                if res.moved:
                    report["moved"] += 1
                report["bytes"] += res.bytes_copied
                return True
            _log("error", f"写入失败 {k}：{res.error}")
            return False

        before_bytes = report["bytes"]
        if _retry_once(_copy, _log, f"写入 {key}"):
            report["applied"] += 1
            cpw.done(key)
        else:
            report["failed"].append(
                {"rel": key, "error": "写入失败（已重试 1 次）"})
        copied = report["bytes"] - before_bytes
        step += 1
        _prog(step, total_steps)

        # 每"真的复制"够一批就让出磁盘/CPU。
        # 注意：同卷移动 bytes_copied=0 —— 没有 IO 就不该暂停，
        # 旧写法按 chunk_bytes 记，等于每搬 24 个文件就睡一次。
        batch += copied or 0
        if batch >= batch_bytes:
            batch = 0
            if batch_pause_ms > 0:
                time.sleep(batch_pause_ms / 1000.0)
            else:
                time.sleep(0)

    cpw.stage("delete", remaining=[p.as_posix() for p in delete_list])

    # 阶段 2：删除（永久删除）
    for rel in delete_list:
        def _delete(r=rel) -> bool:
            t = safe_join(old_root, r)
            if t.is_dir():
                shutil.rmtree(transfer.sys_path(t))
                return True
            if t.exists():
                os.unlink(transfer.sys_path(t))
                return True
            return True          # 本来就不在 = 已达成目的

        if _retry_once(_delete, _log, f"删除 {rel.as_posix()}"):
            report["deleted"] += 1
            cpw.done(rel.as_posix())
        else:
            report["failed"].append(
                {"rel": rel.as_posix(), "error": "删除失败（已重试 1 次）"})
        step += 1
        _prog(step, total_steps)

    # 收尾：落到 verify 阶段。**不代表更新已经完成**——应用后校验由调用方
    # （player_view）执行，校验通过后才由 mark_applied() 写成 applied。
    # 这样"搬运完了但校验途中被强杀"也能在下次启动被发现（旧实现在这里
    # 直接写 applied，scan_checkpoints 会把它当成正常结束静默清掉）。
    cpw.finish("verify")

    _log("info",
         f"完成：应用 {report['applied']}，"
         f"删除 {report['deleted']}，"
         f"跳过 {report['skipped']}，"
         f"失败 {len(report['failed'])}"
         f"（其中瞬时搬运 {report['moved']} 个，"
         f"实际复制 {transfer.human_bytes(report['bytes'])}）")

    return report


def _dir_missing(src: Path, dst: Path, max_files: int = 20000
                 ) -> list[str]:
    """列出 src 里有、但 dst 缺（或大小不符）的文件（包根相对路径）。"""
    missing: list[str] = []
    if not src.is_dir():
        return missing
    n = 0
    for root, _dirs, files in os.walk(transfer.sys_path(src)):
        try:
            sub = Path(root).relative_to(src)
        except ValueError:
            continue
        for f in files:
            n += 1
            if n > max_files:
                return missing
            s = Path(root) / f
            d = dst / sub / f
            try:
                if not d.is_file() or d.stat().st_size != s.stat().st_size:
                    missing.append((sub / f).as_posix())
            except OSError:
                missing.append((sub / f).as_posix())
    return missing


def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(transfer.sys_path(path), "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def verify_after_update(plan: UpdatePlan, old_root: Path,
                        new_root: Path | None = None, *,
                        deep: bool = False,
                        deep_max_file: int = 32 * 1024 * 1024,
                        deep_budget: int = 256 * 1024 * 1024,
                        stats: dict | None = None) -> list[dict]:
    """
    更新后校验：不只查"在不在"，还核对**大小/内容**：
      - copy 项：文件存在且大小符合期望
      - replace_dirs：来源目录里的每个文件都在目标里且大小一致
        （合并策略下目标可以多出玩家自己的文件；"跳过重名"留下的
         玩家版本只要求存在，不比对大小）
      - delete 项：确认已经不在

    期望大小来自 plan.expect_size（生成计划时统计）。老调用方只给了
    new_root 时，退回"和源文件比大小"。
    """
    failed: list[dict] = []
    old_root = Path(old_root)
    src_root = Path(new_root) if new_root is not None else None
    budget = max(0, int(deep_budget))
    deep_done = 0
    deep_bytes = 0

    def _deep_ok(rel: Path) -> tuple[bool, str]:
        """内容级校验（只对"索引里有摘要、且不太大"的文件做，预算内）。"""
        nonlocal budget, deep_done, deep_bytes
        spec = plan.expect_hash.get(rel.as_posix(), "")
        algo, _, want = spec.partition(":")
        if not algo or not want or budget <= 0:
            return True, ""
        target = old_root / rel
        try:
            size = target.stat().st_size
        except OSError as e:
            return False, str(e)
        if size > deep_max_file or size > budget:
            return True, ""
        try:
            got = _hash_file(target, algo)
        except Exception as e:  # noqa: BLE001
            return False, f"读取失败：{e}"
        budget -= size
        deep_done += 1
        deep_bytes += size
        if got.lower() != want.lower():
            return False, f"内容校验失败（{algo} 不一致）"
        return True, ""

    def _expect(rel_key: str) -> int:
        size = plan.expect_size.get(rel_key)
        if size is not None:
            return int(size)
        if src_root is not None:
            try:
                return (src_root / rel_key).stat().st_size
            except OSError:
                return -1
        return -1

    for rel in dict.fromkeys(plan.copy):
        key = rel.as_posix()
        target = old_root / rel
        try:
            if not target.is_file():
                failed.append({"rel": key, "error": "缺失"})
                continue
            size = _expect(key)
            if size >= 0 and target.stat().st_size != size:
                failed.append({"rel": key, "error": "大小与源不一致"})
                continue
            if deep:
                ok, why = _deep_ok(rel)
                if not ok:
                    failed.append({"rel": key, "error": why})
        except OSError as e:
            failed.append({"rel": key, "error": str(e)})

    for rel, _strat in dict.fromkeys(plan.replace_dirs):
        key = rel.as_posix()
        target = old_root / rel
        if not target.is_dir():
            failed.append({"rel": key, "error": "目录缺失"})
            continue
        optional = set(plan.dir_optional.get(key, []))
        rel_files = plan.dir_files.get(key)
        if rel_files is None:
            # 老调用方（没给 source）→ 回退到和源目录逐文件比大小
            if src_root is None:
                continue
            for miss in _dir_missing(src_root / rel, target):
                failed.append({"rel": (rel / miss).as_posix(),
                               "error": "目录内文件缺失或大小不符"})
            continue
        for f in rel_files:
            t = old_root / f
            try:
                if not t.is_file():
                    failed.append({"rel": f, "error": "目录内文件缺失"})
                elif f not in optional:
                    size = plan.expect_size.get(f, -1)
                    if size >= 0 and t.stat().st_size != size:
                        failed.append({"rel": f, "error": "大小与源不一致"})
            except OSError as e:
                failed.append({"rel": f, "error": str(e)})

    for rel in dict.fromkeys(plan.delete):
        try:
            if (old_root / rel).exists():
                failed.append({"rel": rel.as_posix(), "error": "未被删除"})
        except OSError as e:
            failed.append({"rel": rel.as_posix(), "error": str(e)})

    if stats is not None:
        stats["deep_files"] = deep_done
        stats["deep_bytes"] = deep_bytes
    return failed

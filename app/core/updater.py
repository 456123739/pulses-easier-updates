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
  1) copy 文件
  2) replace_dirs 目录操作
  3) delete 文件/目录（走回收站）
"""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CONTENT_DIR_MAP, ChangeKind, ContentType, Strategy
from ..utils.files import safe_join
from . import checkpoint as cp_mod


@dataclass
class UpdatePlan:
    # 文件级操作
    copy: list[Path] = field(default_factory=list)
    delete: list[Path] = field(default_factory=list)
    skip: list[Path] = field(default_factory=list)
    # 目录级操作：(相对路径, 策略)
    replace_dirs: list[tuple[Path, Strategy]] = field(default_factory=list)


def _normalize_keys(mapping: dict) -> dict:
    result: dict[str, object] = {}
    for k, v in mapping.items():
        if isinstance(k, ContentType):
            folder = CONTENT_DIR_MAP.get(k)
            if folder:
                result[folder] = v
        else:
            result[str(k)] = v
    return result


def _expand_dir_files(root: Path | None, rel: Path) -> list[Path]:
    if root is None:
        return []
    base = root / rel
    if not base.is_dir():
        return []
    result: list[Path] = []
    for p in base.rglob("*"):
        if p.is_file():
            try:
                result.append(p.relative_to(root))
            except ValueError:
                continue
    return result


def build_plan(diff, checked, strategies,
               old_root: Path | None = None,
               new_root: Path | None = None) -> UpdatePlan:
    checked_n = _normalize_keys(checked)
    strategies_n = _normalize_keys(strategies)

    plan = UpdatePlan()

    def _resolve_strategy(top: str) -> Strategy:
        raw = strategies_n.get(top, Strategy.FULL_MATCH)
        try:
            return Strategy(raw) if not isinstance(raw, Strategy) else raw
        except ValueError:
            return Strategy.FULL_MATCH

    for change in diff.added + diff.modified + diff.deleted:
        rel = change.rel_path
        parts = rel.parts
        top = parts[0] if parts else ""

        if not checked_n.get(top, False):
            plan.skip.append(rel)
            continue

        strat = _resolve_strategy(top)

        # ---- 文件夹级（非白名单） ----
        if change.is_folder_level:
            if change.kind == ChangeKind.ADDED:
                # 整个目录新增：加入 copy（执行时走目录复制）
                plan.replace_dirs.append((rel, Strategy.FULL_MATCH))
            elif change.kind == ChangeKind.MODIFIED:
                plan.replace_dirs.append((rel, strat))
            elif change.kind == ChangeKind.DELETED:
                if strat == Strategy.FULL_MATCH:
                    plan.delete.extend(_expand_dir_files(old_root, rel))
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

    return plan


# ----------------------------------------------------------------------
# 目录级操作（失败时事务性回滚，成功不留备份）
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


def _replace_dir_full(src: Path, dst: Path, log: Callable[[str, str], None]
                      ) -> bool:
    """
    完全匹配：整目录替换，**失败时事务性回滚**。

    旧实现是 `rmtree(dst)` 再 `copytree`：复制失败（磁盘满/权限/进程被杀）
    玩家的整个目录就永久没了。现在：
        旧目录 --rename--> 同目录临时名 --copytree--> 成功则删临时名
                                            \--失败则 rename 回来
    同卷 rename 是原子操作，所以即便在复制中途被强杀，旧目录也仍在临时名
    下，重跑一次即可；进程正常时不会留下任何备份文件。

    注意：这是**操作级**回滚，不是"撤销上次更新"。成功后旧内容即被丢弃
    （策略叫「完全匹配 / 全部覆盖，含删除」，这是用户明确选择的语义）。
    """
    backup: Path | None = None
    if dst.exists():
        try:
            backup = _unique_sidecar(dst)
            os.replace(dst, backup)
        except Exception as e:  # noqa: BLE001
            log("error", f"替换目录失败 {dst.name}：无法备份旧目录（{e}）")
            return False

    try:
        shutil.copytree(src, dst)
    except Exception as e:  # noqa: BLE001
        log("error", f"替换目录失败 {dst.name}：{e}")
        try:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
        except Exception:  # noqa: BLE001, S110
            pass
        if backup is not None:
            try:
                os.replace(backup, dst)
                log("warn", f"替换目录 {dst.name} 已回滚到替换前的内容")
            except Exception as e2:  # noqa: BLE001
                log("error", f"{dst.name} 自动回滚失败：{e2}；"
                             f"原内容保留在 {backup}")
        return False

    if backup is not None:
        try:
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:  # noqa: BLE001, S110
            pass
    log("info", f"替换目录 {dst.name}")
    return True


def _replace_dir_overwrite(src: Path, dst: Path,
                           log: Callable[[str, str], None]) -> bool:
    """替换重名：目录合并，同名覆盖（只写不删，不产生备份）"""
    try:
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        log("info", f"合并目录 {dst.name}（同名覆盖）")
        return True
    except Exception as e:  # noqa: BLE001
        log("error", f"合并目录失败 {dst.name}：{e}")
        return False


def _replace_dir_skip(src: Path, dst: Path,
                      log: Callable[[str, str], None]) -> bool:
    """跳过重名：目录合并，同名跳过"""
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for root, _dirs, files in os.walk(src):
            rel_dir = Path(root).relative_to(src)
            target_dir = dst / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                s = Path(root) / f
                d = target_dir / f
                if d.exists():
                    continue
                shutil.copy2(s, d)
        log("info", f"合并目录 {dst.name}（同名跳过）")
        return True
    except Exception as e:  # noqa: BLE001
        log("error", f"合并目录失败 {dst.name}：{e}")
        return False


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def execute_plan(
    plan: UpdatePlan,
    old_root: Path,
    new_root: Path,
    log: Callable[[str, str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """
    按计划执行更新。

    删除是**永久删除**（v0.5.0 起不再有回收站）：策略「完全匹配」的语义
    本身就是"含删除"，是否删除由用户在选择策略时决定。
    唯一会做备份的是"目录整体替换"，且只在失败时用于就地回滚。
    """
    old_root = Path(old_root)
    new_root = Path(new_root)

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
    }

    copy_list = list(dict.fromkeys(plan.copy))
    delete_list = list(dict.fromkeys(plan.delete))
    replace_list = list(dict.fromkeys(plan.replace_dirs))

    total_steps = len(copy_list) + len(delete_list) + len(replace_list)
    step = 0

    cp = cp_mod.Checkpoint(
        pack_root=str(old_root),
        stage="apply",
        done=[],
        remaining=[p.as_posix() for p, _ in replace_list],
        new_root=str(new_root),
    )
    cp_mod.save_checkpoint(cp)

    # 阶段 0：目录级替换（先做，保证整块被替换）
    for rel, strat in replace_list:
        src = new_root / rel
        dst = old_root / rel
        if not src.is_dir():
            step += 1
            _prog(step, total_steps)
            continue
        if strat == Strategy.FULL_MATCH:
            ok = _replace_dir_full(src, dst, _log)
        elif strat == Strategy.REPLACE_SAME:
            ok = _replace_dir_overwrite(src, dst, _log)
        else:  # SKIP_SAME
            ok = _replace_dir_skip(src, dst, _log)
        if ok:
            report["applied"] += 1
        else:
            report["failed"].append(
                {"rel": rel.as_posix(), "error": "目录操作失败"})
        step += 1
        _prog(step, total_steps)

    # 阶段 1：文件级新增 + 替换
    cp.stage = "copy"
    cp_mod.save_checkpoint(cp)
    for i, rel in enumerate(copy_list):
        src = new_root / rel
        dst = old_root / rel
        try:
            if not src.is_file():
                step += 1
                _prog(step, total_steps)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            report["applied"] += 1
            cp.done.append(rel.as_posix())
            if rel.as_posix() in cp.remaining:
                cp.remaining.remove(rel.as_posix())
        except Exception as e:  # noqa: BLE001
            report["failed"].append(
                {"rel": rel.as_posix(), "error": str(e)})
            _log("error", f"写入失败 {rel.as_posix()}：{e}")
        step += 1
        _prog(step, total_steps)
        if i % 25 == 24:
            cp_mod.save_checkpoint(cp)

    cp.stage = "delete"
    cp.remaining = [p.as_posix() for p in delete_list]
    cp_mod.save_checkpoint(cp)

    # 阶段 2：删除（永久删除）
    for rel in delete_list:
        try:
            target = safe_join(old_root, rel)
            if target.is_dir():
                shutil.rmtree(target)
                report["deleted"] += 1
            elif target.exists():
                target.unlink()
                report["deleted"] += 1
            else:
                continue
            cp.done.append(rel.as_posix())
        except Exception as e:  # noqa: BLE001
            report["failed"].append(
                {"rel": rel.as_posix(), "error": str(e)})
            _log("error", f"删除失败 {rel.as_posix()}：{e}")
        step += 1
        _prog(step, total_steps)

    # 保留检查点（stage=applied）：只用于判断"上一次更新是否正常结束"，
    # 不提供回滚功能；下一次对同一个包执行更新时会覆盖它。
    cp.stage = "applied"
    cp.remaining = []
    cp_mod.save_checkpoint(cp)

    _log("info",
         f"完成：应用 {report['applied']}，"
         f"删除 {report['deleted']}，"
         f"跳过 {report['skipped']}，"
         f"失败 {len(report['failed'])}")

    return report


def _dir_missing(src: Path, dst: Path, max_files: int = 20000
                 ) -> list[str]:
    """列出 src 里有、但 dst 缺（或大小不符）的文件（包根相对路径）。"""
    missing: list[str] = []
    if not src.is_dir():
        return missing
    n = 0
    for root, _dirs, files in os.walk(src):
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


def verify_after_update(plan: UpdatePlan, old_root: Path,
                        new_root: Path) -> list[dict]:
    """
    更新后校验：不只查"在不在"，还核对**大小/内容**：
      - copy 项：文件存在且大小与源一致
      - replace_dirs：源目录里的每个文件都在目标里且大小一致
        （合并策略下目标可以多出玩家自己的文件）
      - delete 项：确认已经不在
    """
    failed: list[dict] = []
    old_root = Path(old_root)
    new_root = Path(new_root)

    for rel in dict.fromkeys(plan.copy):
        target = old_root / rel
        src = new_root / rel
        try:
            if not target.is_file():
                failed.append({"rel": rel.as_posix(), "error": "缺失"})
            elif src.is_file() and (target.stat().st_size
                                    != src.stat().st_size):
                failed.append({"rel": rel.as_posix(),
                               "error": "大小与源不一致"})
        except OSError as e:
            failed.append({"rel": rel.as_posix(), "error": str(e)})

    for rel, _strat in dict.fromkeys(plan.replace_dirs):
        target = old_root / rel
        src = new_root / rel
        if not target.is_dir():
            failed.append({"rel": rel.as_posix(), "error": "目录缺失"})
            continue
        for miss in _dir_missing(src, target):
            failed.append({"rel": (rel / miss).as_posix(),
                           "error": "目录内文件缺失或大小不符"})

    for rel in dict.fromkeys(plan.delete):
        try:
            if (old_root / rel).exists():
                failed.append({"rel": rel.as_posix(), "error": "未被删除"})
        except OSError as e:
            failed.append({"rel": rel.as_posix(), "error": str(e)})

    return failed

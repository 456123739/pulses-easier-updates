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
from . import trash as trash_mod


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
# 目录级操作
# ----------------------------------------------------------------------
def _unique_backup(dst: Path) -> Path:
    """给 dst 找一个同目录下不存在的备份名（同卷 rename，可回滚）。"""
    base = dst.name + ".pulses_bak"
    cand = dst.with_name(base)
    n = 1
    while cand.exists():
        cand = dst.with_name(f"{base}{n}")
        n += 1
    return cand


def _replace_dir_full(src: Path, dst: Path, log: Callable[[str, str], None],
                      pack_root: Path | None = None,
                      rel: Path | None = None,
                      session: Path | None = None) -> bool:
    """
    完全匹配：旧目录先进回收站 → 整目录复制。

    旧实现是 `rmtree(dst)` 再 `copytree`，一旦复制失败（磁盘满 / 权限 /
    进程被杀）玩家的整个目录就永久没了。现在旧目录先被移走（回收站，
    同卷 rename），复制失败再把旧目录搬回来。

    没有 pack_root/rel 上下文时（直接调用本函数的测试/脚本），退化为
    「同目录改名备份」。
    """
    backup: Path | None = None
    trashed: Path | None = None

    if dst.exists():
        if pack_root is not None and rel is not None:
            res = trash_mod.move_to_trash(pack_root, [rel], session=session)
            if res["moved"]:
                trashed = Path(res["dir"])
            else:
                reason = (res["failed"][0]["error"] if res["failed"]
                          else "回收站不可用")
                log("error",
                    f"替换目录失败 {dst.name}：旧目录无法移入回收站"
                    f"（{reason}）")
                return False
        else:
            try:
                backup = _unique_backup(dst)
                os.replace(dst, backup)
            except Exception as e:  # noqa: BLE001
                log("error", f"替换目录失败 {dst.name}：无法备份旧目录（{e}）")
                return False

    try:
        shutil.copytree(src, dst)
    except Exception as e:  # noqa: BLE001
        log("error", f"替换目录失败 {dst.name}：{e}")
        # 回滚：清掉半成品，把旧目录搬回来
        try:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
        except Exception:  # noqa: BLE001, S110
            pass
        restored = False
        if trashed is not None and rel is not None:
            restored = trash_mod.restore_entry(trashed, rel)
        elif backup is not None:
            try:
                os.replace(backup, dst)
                restored = True
            except Exception:  # noqa: BLE001, S110
                restored = False
        if not restored:
            log("error", f"{dst.name} 的旧内容已备份，但自动回滚失败；"
                         f"备份位置：{trashed or backup}")
        else:
            log("warn", f"替换目录 {dst.name} 已回滚到替换前的内容")
        return False

    if backup is not None:
        try:
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:  # noqa: BLE001, S110
            pass
    log("info", f"替换目录 {dst.name}（旧目录已备份到回收站）")
    return True


def _replace_dir_overwrite(src: Path, dst: Path,
                           log: Callable[[str, str], None],
                           pack_root: Path | None = None,
                           rel: Path | None = None,
                           session: Path | None = None) -> bool:
    """
    替换重名：目录合并，同名覆盖。

    被覆盖的旧文件先备份到回收站（同一会话），因此该策略不会永久丢数据。
    只在 dst 里已存在同名文件时才产生备份，不会无谓地复制整个目录。
    """
    backups: list[Path] = []
    try:
        dst.mkdir(parents=True, exist_ok=True)

        if (pack_root is not None and rel is not None
                and session is not None and src.is_dir()):
            for root, _dirs, files in os.walk(src):
                try:
                    sub = Path(root).relative_to(src)
                except ValueError:
                    continue
                for f in files:
                    target_rel = Path(rel) / sub / f
                    try:
                        if (pack_root / target_rel).is_file():
                            backups.append(target_rel)
                    except OSError:
                        continue
            if backups:
                trash_mod.move_to_trash(pack_root, backups, session=session)

        shutil.copytree(src, dst, dirs_exist_ok=True)
        log("info", f"合并目录 {dst.name}（同名覆盖，"
                    f"备份 {len(backups)} 个被覆盖文件）")
        return True
    except Exception as e:  # noqa: BLE001
        log("error", f"合并目录失败 {dst.name}：{e}")
        # 回滚被移走的旧文件
        for target_rel in backups:
            try:
                trash_mod.restore_entry(session, target_rel)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001, S110
                pass
        return False


def _replace_dir_skip(src: Path, dst: Path,
                      log: Callable[[str, str], None],
                      pack_root: Path | None = None,
                      rel: Path | None = None) -> bool:
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
    use_trash: bool = True,
) -> dict:
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
        "trash_dir": None,
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

    # 本次更新的回收站会话：所有备份（目录替换 + 删除）共用一个会话，
    # 便于整批回滚，也避免同一秒内的多次操作互相覆盖 manifest。
    trash_session: Path | None = None

    def _ensure_session() -> Path:
        nonlocal trash_session
        if trash_session is None:
            trash_session = trash_mod.new_session(old_root)
        return trash_session

    # 阶段 0：目录级替换（先做，保证整块被替换）
    for rel, strat in replace_list:
        src = new_root / rel
        dst = old_root / rel
        if not src.is_dir():
            step += 1
            _prog(step, total_steps)
            continue
        if strat == Strategy.FULL_MATCH:
            session = _ensure_session() if dst.exists() else None
            ok = _replace_dir_full(src, dst, _log, pack_root=old_root,
                                   rel=rel, session=session)
        elif strat == Strategy.REPLACE_SAME:
            session = _ensure_session() if dst.is_dir() else None
            ok = _replace_dir_overwrite(src, dst, _log, pack_root=old_root,
                                        rel=rel, session=session)
        else:  # SKIP_SAME
            ok = _replace_dir_skip(src, dst, _log, pack_root=old_root,
                                   rel=rel)
        if ok:
            report["applied"] += 1
        else:
            report["failed"].append(
                {"rel": rel.as_posix(), "error": "目录操作失败"})
        step += 1
        _prog(step, total_steps)

    # 阶段 1：文件级新增 + 替换
    for rel in copy_list:
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

    cp.stage = "delete"
    cp.remaining = [p.as_posix() for p in delete_list]
    cp_mod.save_checkpoint(cp)

    # 阶段 2：删除（回收站）
    if delete_list:
        if use_trash:
            tr = trash_mod.move_to_trash(old_root, delete_list,
                                         session=_ensure_session())
            report["trash_dir"] = str(tr["dir"])
            for item in tr["moved"]:
                report["deleted"] += 1
                cp.done.append(item["rel"])
            for item in tr["failed"]:
                report["failed"].append(
                    {"rel": item["rel"], "error": item["error"]})
        else:
            for rel in delete_list:
                try:
                    target = safe_join(old_root, rel)
                    if target.exists():
                        target.unlink()
                    report["deleted"] += 1
                    cp.done.append(rel.as_posix())
                except Exception as e:  # noqa: BLE001
                    report["failed"].append(
                        {"rel": rel.as_posix(), "error": str(e)})

        step += len(delete_list)
        _prog(step, total_steps)

    cp.stage = "done"
    cp_mod.clear_checkpoint(old_root)

    if trash_session is not None:
        report["trash_dir"] = str(trash_session)

    _log("info",
         f"完成：应用 {report['applied']}，"
         f"删除 {report['deleted']}，"
         f"跳过 {report['skipped']}，"
         f"失败 {len(report['failed'])}")

    return report


def verify_after_update(plan: UpdatePlan, old_root: Path,
                        new_root: Path) -> list[dict]:
    """更新后校验：确认 copy 项已就位；目录级 replace_dirs 检查存在"""
    failed: list[dict] = []
    old_root = Path(old_root)
    for rel in plan.copy:
        target = old_root / rel
        try:
            if not target.is_file():
                failed.append({"rel": rel.as_posix(), "error": "缺失"})
        except OSError as e:
            failed.append({"rel": rel.as_posix(), "error": str(e)})
    for rel, _ in plan.replace_dirs:
        target = old_root / rel
        if not target.is_dir():
            failed.append({"rel": rel.as_posix(), "error": "目录缺失"})
    return failed
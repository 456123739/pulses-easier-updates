"""
apply_rules.py — 「这条变更是应用还是跳过」的唯一判定
------------------------------------------------
历史上这套判断有三份互不一致的实现：

  - `updater.build_plan`                     （决定真正执行什么）
  - `player_view._collect_download_tasks_from_diff`（决定要下载什么）
  - `change_list._will_skip`                 （决定界面上灰显什么）

三处一旦不一致，就会出现"界面说会跳过、实际却应用了"或者
"界面说要更新、却既没下载也不会应用"。现在统一到这里：

    勾选（checked）：缺省视为**未勾选 = 不应用**（与界面默认一致）
    策略（strategies）：未知/非法值一律回退到"完全匹配"（最保守，
                        因为它至少不会"看起来跳过其实覆盖"）

判定总表：

  文件级（白名单目录里的文件、根目录散文件）
    ADDED                       → 应用（复制/下载）
    MODIFIED   + 跳过重名        → 跳过
    MODIFIED   + 其他策略        → 应用
    DELETED    + 完全匹配        → 删除（应用）
    DELETED    + 其他策略        → 跳过

  文件夹级（非白名单目录整体）
    ADDED                       → 应用（整目录复制）
    MODIFIED   + 任意策略        → 应用（替换/合并，都不会删玩家文件）
    DELETED    + 完全匹配        → 删除整目录
    DELETED    + 其他策略        → 跳过

未勾选的顶层项 → 整项跳过（不参与更新）。
"""

from pathlib import Path

from ..config import CONTENT_DIR_MAP, ChangeKind, ContentType, Strategy


def normalize_keys(mapping: dict | None) -> dict:
    """
    把 ContentType 键翻译成目录名（兼容旧调用方），其余原样保留。

    已经是纯字符串键时**直接返回原对象**（零拷贝）——这个函数处在
    每次策略变更都要跑一遍的热路径上（几千条变更 × 每次重绘）。
    """
    if not mapping:
        return {}
    for k in mapping:
        if not isinstance(k, str):
            break
    else:
        return mapping
    result: dict[str, object] = {}
    for k, v in mapping.items():
        if isinstance(k, ContentType):
            folder = CONTENT_DIR_MAP.get(k)
            if folder:
                result[folder] = v
        else:
            result[str(k)] = v
    return result


def resolve_strategy_n(strategies_n: dict, top: str) -> Strategy:
    """已归一化映射的快速版本（内部热路径用）。"""
    raw = strategies_n.get(top, Strategy.FULL_MATCH)
    if isinstance(raw, Strategy):
        return raw
    try:
        return Strategy(raw)
    except ValueError:
        return Strategy.FULL_MATCH


def is_checked_n(checked_n: dict, top: str, default: bool = False) -> bool:
    """已归一化映射的快速版本（内部热路径用）。"""
    if not checked_n:
        return default
    return bool(checked_n.get(top, default))


def resolve_strategy(strategies: dict | None, top: str) -> Strategy:
    """取某个顶层项的策略；未知/非法一律回退"完全匹配"。"""
    return resolve_strategy_n(normalize_keys(strategies), top)


def is_checked(checked: dict | None, top: str,
               default: bool = False) -> bool:
    """顶层项是否被勾选。空映射（尚未建表）时按 default 处理。"""
    return is_checked_n(normalize_keys(checked), top, default)


def top_of(rel) -> str:
    parts = rel.parts if hasattr(rel, "parts") else Path(rel).parts
    return parts[0] if parts else ""


def will_skip(change, checked: dict | None, strategies: dict | None,
              checked_default: bool = False) -> bool:
    """这条变更在当前勾选/策略下会被跳过吗？"""
    top = top_of(change.rel_path)
    if not is_checked(checked, top, default=checked_default):
        return True

    strat = resolve_strategy(strategies, top)

    if change.is_folder_level:
        if change.kind == ChangeKind.DELETED:
            return strat != Strategy.FULL_MATCH
        # ADDED / MODIFIED：目录级操作都不会删掉玩家本地文件
        return False

    if change.kind == ChangeKind.MODIFIED:
        return strat == Strategy.SKIP_SAME
    if change.kind == ChangeKind.DELETED:
        return strat != Strategy.FULL_MATCH
    return False          # ADDED

"""
export_rules.py — 导出规则引擎（PCL 语义，逐条对齐）
------------------------------------------------
移植自 PCL2 `PageInstanceExport`（2.13.1.1，见 docs/PCL-整合包导出-技术规范.md §1）。

为什么单独一个模块：规则判定决定"哪些文件进包"，它错一次就是丢文件/多打包，
所以这里只放纯函数，全部有单测（tests/test_export_rules.py）。

规则语义（与 PCL 完全一致）：
- 一行一条，路径相对实例根；
- `/` 归一成 `\\`；
- `!` 开头 = 排除，否则 = 包含；
- **靠后的规则覆盖靠前的**（不是"命中即停"）；
- 支持 VB `Like` 通配：`*` `?` `#`（数字）`[abc]` `[!abc]`；
- 目录规则（以 `\\` 结尾）自动补 `*`；
- 比较大小写不敏感；
- 空行、`#` 注释、`=` 开头行忽略。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ----------------------------------------------------------------------
# PCL 默认选项组（转录 PageInstanceExport.xaml:41-290）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OptionGroup:
    """一个「导出内容」勾选项。"""

    key: str
    title: str
    rules: tuple[str, ...] = ()
    default: bool = True
    requires_loader: bool = False
    requires_optifine: bool = False
    show_only: bool = False          # ShowRules：只用于显示过滤，不直接产出规则
    children: tuple["OptionGroup", ...] = field(default=())


# 固定追加的排除段（PCL 永远放在最后，优先级最高）
FIXED_EXCLUDES: tuple[str, ...] = (
    "!*.log",
    "!*.dat_old",
    "!*.BakaCoreInfo",
    "!hmclversion.cfg",
    "!log4j2.xml",
)

DEFAULT_OPTIONS: tuple[OptionGroup, ...] = (
    OptionGroup("options", "游戏本体设置", ("options.txt", "configureddefaults/")),
    OptionGroup("personal", "游戏本体个人信息",
                ("hotbar.nbt", "command_history.txt"), default=False),
    OptionGroup("optifine", "OptiFine 设置",
                ("optionsof.txt", "optionsshaders.txt"), requires_optifine=True),
    OptionGroup("mods", "Mod", (
        "mods/", "!mods/*.disabled", "!mods/*.old", "!mods/.connector/",
        "coremods/", "lib/"), requires_loader=True, children=(
        OptionGroup("mods-disabled", "已禁用的 Mod",
                    ("mods/*.disabled", "mods/*.old"), default=False),
        OptionGroup("mods-data", "整合包重要数据", (
            "hotai/", "bansoukou/", "addons/", "multiblocked/",
            "modpack-update-checker/", "global_packs/", "global_resource_packs/",
            "global_data_packs/", "optional_data_packs/",
            "moonlight-global-datapacks/", "maps/", "icon.png",
            "mods-resourcepacks/", "matmos/", "resource_assorts/",
            "resource_assorts.json", "patchouli_books/", "datapacks/",
            "kubejs*/", "!kubejs*/probe/", "!kubejs*/exported/",
            "!kubejs*/jsconfig.json", "!kubejs*/README.txt", "openloader/",
            "worldshape/", "resources/", "scripts/", "structures/",
            "fontfiles/", "oresources/", "packmenu/", "craftpresence/",
            "pointblanks/", "template*/", "!template*/playerdata/",
            "!template*/stats/")),
        OptionGroup("mods-config", "Mod 设置", (
            "config/", "!config/jei/world/", "!config/worldedit/",
            "config/worldedit/worldedit.properties", "!config/spark/",
            "config/spark/config.json", "defaultconfigs/",
            "journeymap/config/", "journeymap/server/",
            "TrashSlotSaveState.json", "customfov.txt", "gg.essential.mod/",
            "essential/", "!essential/*/", "!essential/*.jar*",
            "!essential/screenshot-checksum-caches.json",
            "!essential/microsoft_accounts.json", "paragliderSettings.nbt",
            "local/client_config.json", "local/ftbl.json",
            "local/client/sidebar_buttons.json", "local/client/ftbutilities.cfg",
            "local/client/ftblib.cfg", "local/client/xencraft.cfg",
            "liteloader.properties", "default_reference.xml",
            "CustomSkinLoader/CustomSkinLoader.json", "!config/tacz/custom/")),
        OptionGroup("tacz", "TaCZ 枪包", ("tacz/", "config/tacz/custom/")),
        OptionGroup("paintings", "已上传的沉浸画", ("immersive_paintings/",)),
        OptionGroup("maps", "已绘制的地图",
                    ("journeymap/data/", "xaero/", "XaeroWaypoints/",
                     "XaeroWorldMap/"), default=False),
        OptionGroup("jei", "JEI 个人信息", ("config/jei/world/",), default=False),
        OptionGroup("emi", "EMI 个人信息", ("emi.json",), default=False),
        OptionGroup("patchouli", "帕秋莉手册个人信息", ("patchouli_data.json",),
                    default=False),
    )),
    OptionGroup("resourcepacks", "资源包", ("resourcepacks/", "texturepacks/"),
                show_only=True, requires_loader=True),
    OptionGroup("shaderpacks", "光影包", ("shaderpacks/",), show_only=True,
                requires_loader=True),
    OptionGroup("screenshots", "截图", ("screenshots/",), default=False),
    OptionGroup("schematics", "导出的结构", ("schematics/",), default=False),
    OptionGroup("replay", "录像回放",
                ("replay_recordings/", "replay_videos/"), default=False,
                requires_loader=True),
    OptionGroup("saves", "单人游戏存档", ("saves/",), default=False,
                show_only=True),
    OptionGroup("licence", "协议", ("LICEN*",)),
    OptionGroup("servers", "多人游戏服务器列表", ("servers.dat",), default=False),
)

# 动态生成子项的目录（resourcepacks/texturepacks/saves/shaderpacks）
DYNAMIC_FOLDERS = ("resourcepacks", "texturepacks", "saves", "shaderpacks")

# PCL 的子项黑名单（名字包含即跳过）
SUB_OPTION_BLACKLIST = ("Quark Programmer Art.zip", "+ EuphoriaPatches_",
                        "PCL2 Skin.zip")


# ----------------------------------------------------------------------
# 规则语法
# ----------------------------------------------------------------------

_LIKE_CACHE: dict[str, re.Pattern[str]] = {}


def like_to_regex(pattern: str) -> re.Pattern[str]:
    """VB `Like` → 正则（只支持 PCL 用到的子集：* ? # [] [!]）。"""
    cached = _LIKE_CACHE.get(pattern)
    if cached is not None:
        return cached
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "#":
            out.append(r"\d")
        elif ch == "[":
            j = pattern.find("]", i + 1)
            if j < 0:                      # 不闭合：当普通字符
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1:j]
                negate = body.startswith("!")
                if negate:
                    body = body[1:]
                out.append("[" + ("^" if negate else "") +
                           re.escape(body).replace(r"\-", "-") + "]")
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    compiled = re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)
    _LIKE_CACHE[pattern] = compiled
    return compiled


def standardize_lines(raw, add_suffix_star: bool = True) -> list[str]:
    """PCL `StandardizeLines` 的等价实现。"""
    out: list[str] = []
    for line in raw or ():
        text = str(line).strip()
        if not text or text.startswith("#") or text.startswith("="):
            continue
        text = text.replace("/", "\\")
        if add_suffix_star and text.endswith("\\"):
            text += "*"
        out.append(text)
    return out


def should_keep(rel_path: str, rules) -> bool:
    """最后一条命中的规则决定去留（PCL `PageInstanceExport.xaml.vb:508-512`）。"""
    rel = str(rel_path).replace("/", "\\").lower()
    keep = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        if like_to_regex(pattern.lower()).match(rel):
            keep = not negate
    return keep


def default_keys() -> set[str]:
    """默认勾选的键集合（含默认勾选的子项）。"""
    keys: set[str] = set()
    for group in DEFAULT_OPTIONS:
        if group.default:
            keys.add(group.key)
        for child in group.children:
            if child.default:
                keys.add(child.key)
    return keys


def expand_rules(selected: set[str] | None = None,
                 extra: tuple[str, ...] = ()) -> list[str]:
    """把勾选的选项组展开成规则串（默认全选 + 固定排除段）。

    `show_only` 的组（资源包/光影/存档）在 PCL 里靠动态子项出规则；
    这里允许整组勾选直接展开成 `<目录>/*`，UI 更简单，效果等价于"整目录导出"。
    """
    chosen = default_keys() if selected is None else set(selected)
    rules: list[str] = []
    for group in DEFAULT_OPTIONS:
        if group.key in chosen or any(c.key in chosen for c in group.children):
            # 父项被选中时先放父规则，再放子项（子项靠后 → 覆盖父项）
            if group.key in chosen:
                rules.extend(group.rules)
            for child in group.children:
                if child.key in chosen:
                    rules.extend(child.rules)
    rules.extend(extra)
    rules.extend(FIXED_EXCLUDES)
    return standardize_lines(rules)

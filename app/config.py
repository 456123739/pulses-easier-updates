"""
config.py — 全局配置与运行时常量
------------------------------------------------
存放与业务相关、但不属于视觉层的常量：
更新内容类型、策略枚举、更新包元数据字段名等。
"""

from enum import Enum


# ----------------------------------------------------------------------
# 更新内容类型（对应玩家端勾选项）
# ----------------------------------------------------------------------
class ContentType(str, Enum):
    MODS          = "mods"
    CONFIG        = "config"
    SAVES         = "saves"
    RESOURCEPACKS = "resourcepacks"
    OTHER         = "其他"

    @property
    def label(self) -> str:
        """用于 UI 显示的名称"""
        return self.value

# 内容类型在整合包根目录下的实际文件夹名映射
CONTENT_DIR_MAP = {
    ContentType.MODS:          "mods",
    ContentType.CONFIG:        "config",
    ContentType.SAVES:         "saves",
    ContentType.RESOURCEPACKS: "resourcepacks",
    ContentType.OTHER:         None,   # None 代表根目录下其余内容
}

# 兼容：旧版 option_panel 用过的"默认勾选"表已随该模块删除；
# 现在玩家端勾选状态由策略表逐项驱动（见 player_view._apply_strategies）。

# ----------------------------------------------------------------------
# 更新策略
# ----------------------------------------------------------------------
class Strategy(str, Enum):
    FULL_MATCH   = "完全匹配"   # 全部覆盖，含删除
    REPLACE_SAME = "替换重名"   # 仅覆盖重名文件，不删除
    SKIP_SAME    = "跳过重名"   # 跳过已存在文件
    MERGE_KEYS   = "保留键位"   # 覆盖内容，但本地 key_* 行原样保留
    SMART_EXCLUDE = "智能剔除"  # 覆盖内容，但优化/渲染类模组的配置保持本地版本

    @property
    def label(self) -> str:
        return self.value

# 目录级可选策略：整目录替换/合并，没有"键位"概念
STRATEGY_LIST = [Strategy.FULL_MATCH, Strategy.REPLACE_SAME, Strategy.SKIP_SAME]
# 根目录散文件多一档「保留键位」
FILE_STRATEGY_LIST = STRATEGY_LIST + [Strategy.MERGE_KEYS]
# config 多一档「智能剔除」：优化/渲染类模组的配置保持玩家本地版本
# （归属表见 core/config_exclude.py；这个概念只在 config 上有意义，不要外溢）
CONFIG_STRATEGY_LIST = STRATEGY_LIST + [Strategy.SMART_EXCLUDE]

# 每种内容类型的默认策略
DEFAULT_STRATEGY = {
    ContentType.MODS:          Strategy.FULL_MATCH,
    ContentType.CONFIG:        Strategy.REPLACE_SAME,
    ContentType.SAVES:         Strategy.SKIP_SAME,
    ContentType.RESOURCEPACKS: Strategy.REPLACE_SAME,
    ContentType.OTHER:         Strategy.REPLACE_SAME,
}

# 走"文件级比对"的内容目录：默认必须能删除"新版已移除"的条目，
# 否则旧 mod 会永远留在玩家目录里。
FILE_LEVEL_DIRS = frozenset({"mods", "resourcepacks", "shaderpacks", "tacz"})

# 目录名 → 默认策略（运行时唯一事实来源：玩家端默认策略表从这里取）
DEFAULT_STRATEGY_BY_DIR: dict[str, Strategy] = {
    dirname: DEFAULT_STRATEGY[ct]
    for ct, dirname in CONTENT_DIR_MAP.items() if dirname
}
for _d in FILE_LEVEL_DIRS:
    DEFAULT_STRATEGY_BY_DIR[_d] = Strategy.FULL_MATCH
del _d


def default_strategy_for_dir(top: str) -> Strategy:
    """顶层目录的默认策略；未知目录一律"替换重名"（合并覆盖，不删文件）。"""
    return DEFAULT_STRATEGY_BY_DIR.get(top, DEFAULT_STRATEGY[ContentType.OTHER])


# 只有这些顶层目录提供「智能剔除」（配置文件的归属判定只对 config 有意义）
SMART_EXCLUDE_DIRS = frozenset({"config"})


def strategy_list_for(top: str, is_file: bool = False) -> list[Strategy]:
    """
    某一行的可选策略（界面按钮、导入校验、推荐值校验共用同一份）。

    - 根目录散文件：四档（多「保留键位」）
    - config：四档（多「智能剔除」）
    - 其它目录：三档
    """
    if is_file:
        return FILE_STRATEGY_LIST
    if str(top).strip().lower() in SMART_EXCLUDE_DIRS:
        return CONFIG_STRATEGY_LIST
    return STRATEGY_LIST


# ----------------------------------------------------------------------
# 键位保护
#   原版 + 绝大多数 Forge/Fabric 模组的键位都写在根目录 options.txt 的
#   key_* 行里（模组注册 KeyBinding，游戏整表写回同一个文件）。
#   这类文件默认走「保留键位」：包里的内容照常覆盖（新版默认值生效），
#   玩家本地已有的 key_* 行原样保留。
# ----------------------------------------------------------------------
KEYBIND_FILES = frozenset({"options.txt"})

# 玩家端策略表里**默认禁用**的根目录散文件（进了「已禁用」折叠区，不参与更新）。
# options.txt 里全是玩家本地设置（键位/画质/音量），更新它收益低、风险高。
DEFAULT_DISABLED_FILES = frozenset({"options.txt"})

# 开发者打包时**永远不带**的根目录散文件（即使勾选了也不打进去）
NEVER_PACK_FILES = frozenset({"options.txt"})


def default_strategy_for_file(name: str) -> Strategy:
    """根目录散文件的默认策略（键位文件走保留键位，其余替换重名）。"""
    if str(name).strip().lower() in KEYBIND_FILES:
        return Strategy.MERGE_KEYS
    return default_strategy_for_dir("")



# ----------------------------------------------------------------------
# 定位整合包：判定根目录的标志文件夹
# ----------------------------------------------------------------------
ROOT_MARKER_DIR = "mods"

# ----------------------------------------------------------------------
# 只属于本地、永不属于更新内容的文件/目录
#   - 更新未完成凭证（软件写的）
#   - 旧版回收站（如果还在）
# 比对时一律忽略，避免软件自己写的文件被当成"更新内容"或被删掉。
# ----------------------------------------------------------------------
LOCAL_ONLY_DIRS = frozenset({".pulses_easier", ".pulses_trash"})
LOCAL_ONLY_FILES = frozenset({
    "!!!更新未完成-请阅读!!!.txt",
    "pending_update.json",
})


def is_transient_name(name: str) -> bool:
    """搬运/替换过程中留下的临时名（不能参与比对）。"""
    n = str(name)
    return n.endswith(".pulses_new") or ".pulses_tmp" in n


def is_local_only_name(name: str) -> bool:
    n = str(name)
    return n in LOCAL_ONLY_FILES or is_transient_name(n)


def is_local_only_dir(name: str) -> bool:
    return str(name) in LOCAL_ONLY_DIRS


# 变更类型
class ChangeKind(str, Enum):
    ADDED    = "added"
    MODIFIED = "modified"
    DELETED  = "deleted"
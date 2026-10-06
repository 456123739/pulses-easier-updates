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

    @property
    def label(self) -> str:
        return self.value

STRATEGY_LIST = [Strategy.FULL_MATCH, Strategy.REPLACE_SAME, Strategy.SKIP_SAME]

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


def is_local_only_name(name: str) -> bool:
    return str(name) in LOCAL_ONLY_FILES


def is_local_only_dir(name: str) -> bool:
    return str(name) in LOCAL_ONLY_DIRS


# 变更类型
class ChangeKind(str, Enum):
    ADDED    = "added"
    MODIFIED = "modified"
    DELETED  = "deleted"
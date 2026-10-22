"""
config_exclude.py — 「智能剔除」：哪些 config 文件属于优化 / 渲染类模组
------------------------------------------------
「智能剔除」是 config 的第四档策略：

    更新照常合并 config，但**属于客户端优化模组和渲染器的配置文件保持
    玩家本地版本**（本地没有的才写入包里的默认值）。

为什么需要它
    这些模组的配置项是按**玩家的机器**调的（GPU 型号、显存、渲染距离、
    分配内存、mixin 开关……）。整合包更新把作者机器上的默认值推下来，
    轻则画面变糊、帧数下降，重则直接崩游戏——而玩家往往不知道是自己
    的配置被覆盖了。

判定方式（不依赖网络、不依赖 jar 文件名）
    config 里的文件几乎都以**模组 id** 命名，于是按路径反查归属：

        config/sodium-options.json          → sodium      （去掉 options）
        config/embeddium-options.json       → embeddium
        config/DistantHorizons.toml         → distanthorizons
        config/modernfix-mixins.properties  → modernfix   （去掉 mixins）
        config/sodium/xxx.json              → sodium      （子目录名）
        config/lithium.properties           → lithium
        config/particle_core_config_v1.json → particlecore（去掉 config/v1）

    比对前统一规范化（小写、去掉 `- _ . 空格` 等分隔符），所以
    `dynamic_fps` / `DynamicFPS` / `dynamic-fps` 都算同一个。

收录标准（宁可多一条，不可漏一条）
    1. 渲染器 / 光影加载器：Sodium 系、Iris/Oculus/OptiFine、VulkanMod、
       Canvas、Nothirium，以及只服务于渲染器的配套模组（Sodium Extra 等）；
    2. 客户端优化模组：直接决定画面质量 / 帧数 / 内存占用的剔除、批处理、
       粒子、方块实体、LOD、分辨率缩放、启动与模型加载类模组；
    3. 配置项通常是**按机器调**的（渲染距离、GPU 特性、线程数、mixin 开关）。

    不收录：纯服务端优化（ServerCore、VMP…）、网络与封包、启动器/界面工具、
    纯修复类、装饰与模型材质类——它们的配置与玩家机器无关，跟着整合包走
    才是对的。

维护方式
    往 TABLE 里加一行即可（`key` 用小写字母数字，别名放进 `aliases`）。
    新增条目后跑 `python -m unittest tests.test_config_exclude`，测试会检查
    表本身是否规范（重复、未规范化、缺分类）。
"""

from dataclasses import dataclass
from pathlib import Path

# 分类：只影响日志/界面里的措辞
KIND_RENDER = "渲染器"
KIND_PERF = "优化"

# 表里数据的核对时间（人工复核用；不参与任何逻辑）
LAST_CHECKED = "2026-10-22"


@dataclass(frozen=True)
class ConfigOwner:
    """一个"配置该被保护"的模组。"""

    key: str                      # 主 id（小写，可含 - _ .）
    name: str                     # 显示名
    kind: str                     # KIND_RENDER / KIND_PERF
    aliases: tuple[str, ...] = ()  # 其它可能出现在配置名里的 id（已规范化）
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}（{self.kind}）"


# ----------------------------------------------------------------------
# 模组表
#   数据来源：Modrinth `optimization` 分类按下载量排序的前 200 项，
#   逐个核对 client_side / server_side 与 jar 内的 mod id、配置文件名，
#   再按上面的收录标准筛出下面这些。
# ----------------------------------------------------------------------
TABLE: tuple[ConfigOwner, ...] = (
    # ---- 渲染器 / 光影加载器 -----------------------------------------
    ConfigOwner("sodium", "Sodium（钠）", KIND_RENDER),
    ConfigOwner("iris", "Iris Shaders（光影）", KIND_RENDER,
                aliases=("irisshaders",)),
    ConfigOwner("oculus", "Oculus（光影）", KIND_RENDER),
    ConfigOwner("optifine", "OptiFine", KIND_RENDER,
                # 它的配置在根目录 optionsof.txt / optionsshaders.txt，
                # 不在 config 里；列在这里是为了记录"它属于渲染器"
                note="配置在根目录，不在 config 内"),
    ConfigOwner("embeddium", "Embeddium（铷）", KIND_RENDER),
    ConfigOwner("rubidium", "Rubidium（铷·旧版）", KIND_RENDER),
    ConfigOwner("magnesium", "Magnesium（镁）", KIND_RENDER),
    ConfigOwner("xenon", "Xenon（氙）", KIND_RENDER,
                aliases=("xenonforge",)),
    ConfigOwner("vulkanmod", "VulkanMod", KIND_RENDER),
    ConfigOwner("chloride", "Chloride", KIND_RENDER),
    ConfigOwner("flerovium", "Flerovium", KIND_RENDER),
    ConfigOwner("canvas", "Canvas 渲染器", KIND_RENDER,
                aliases=("canvasrenderer",)),
    ConfigOwner("nothirium", "Nothirium", KIND_RENDER),
    ConfigOwner("nvidium", "Nvidium", KIND_RENDER),
    ConfigOwner("sodium-extra", "Sodium Extra", KIND_RENDER),
    ConfigOwner("sodium-extras", "Sodium Extras", KIND_RENDER),
    ConfigOwner("sodium-options-api", "Sodium Options API", KIND_RENDER),
    ConfigOwner("sodium-dynamic-lights", "Sodium Dynamic Lights", KIND_RENDER),
    ConfigOwner("sodiumleafculling", "Sodium Leaf Culling", KIND_RENDER),
    ConfigOwner("embeddium-plus", "Embeddium Plus", KIND_RENDER),
    ConfigOwner("rubidium-extra", "Embeddium（Rubidium）Extra", KIND_RENDER,
                aliases=("embeddiumextra",)),
    ConfigOwner("distanthorizons", "Distant Horizons（远处地平线）",
                KIND_RENDER, note="LOD 渲染，配置里全是显存/画质选项"),
    ConfigOwner("voxy", "Voxy（LOD 渲染）", KIND_RENDER,
                note="和 Distant Horizons 同类的远处渲染"),
    ConfigOwner("better-clouds", "Better Clouds", KIND_RENDER),
    ConfigOwner("bettergrassify", "BetterGrassify", KIND_RENDER,
                aliases=("bettergrass",)),

    # ---- 客户端优化 ---------------------------------------------------
    ConfigOwner("immediatelyfast", "ImmediatelyFast", KIND_PERF),
    ConfigOwner("entityculling", "Entity Culling", KIND_PERF),
    ConfigOwner("moreculling", "More Culling", KIND_PERF),
    ConfigOwner("dynamic-fps", "Dynamic FPS", KIND_PERF),
    ConfigOwner("badoptimizations", "BadOptimizations", KIND_PERF),
    ConfigOwner("ferrite-core", "FerriteCore", KIND_PERF),
    ConfigOwner("krypton", "Krypton", KIND_PERF, aliases=("kryptonfnp",)),
    ConfigOwner("lithium", "Lithium", KIND_PERF),
    ConfigOwner("modernfix", "ModernFix", KIND_PERF,
                aliases=("modernfixmvus",)),
    ConfigOwner("memoryleakfix", "Memory Leak Fix", KIND_PERF),
    ConfigOwner("debugify", "Debugify", KIND_PERF),
    ConfigOwner("c2me", "C2ME（并发区块管理）", KIND_PERF,
                aliases=("c2mefabric", "c2meneoforge",)),
    ConfigOwner("modelfix", "Model Gap Fix", KIND_PERF,
                aliases=("modelgapfix",)),
    ConfigOwner("ebe", "Enhanced Block Entities", KIND_PERF,
                aliases=("enhancedblockentities",)),
    ConfigOwner("obe", "Optimized Block Entities", KIND_PERF,
                aliases=("optimizedblockentities",)),
    ConfigOwner("better-block-entities", "Better Block Entities", KIND_PERF,
                aliases=("bbeconfig", "bbe"),
                note="配置文件叫 BBEConfig.json，没有完整模组名"),
    ConfigOwner("particle-core", "Particle Core", KIND_PERF),
    ConfigOwner("scalablelux", "ScalableLux", KIND_PERF),
    ConfigOwner("starlight", "Starlight", KIND_PERF,
                aliases=("starlightforge",)),
    ConfigOwner("lazydfu", "LazyDFU", KIND_PERF),
    ConfigOwner("fastload", "Fastload", KIND_PERF),
    ConfigOwner("threadtweak", "ThreadTweak", KIND_PERF),
    ConfigOwner("cull-leaves", "Cull Leaves", KIND_PERF),
    ConfigOwner("cull-less-leaves", "Cull Less Leaves", KIND_PERF,
                aliases=("culllessleavesreforged",)),
    ConfigOwner("fast-paintings", "Fast Paintings", KIND_PERF),
    ConfigOwner("better-beds", "Better Beds", KIND_PERF),
    ConfigOwner("asyncparticles", "AsyncParticles", KIND_PERF),
    ConfigOwner("fps-reducer", "FPS Reducer", KIND_PERF),
    ConfigOwner("exordium", "Exordium", KIND_PERF),
    ConfigOwner("redirected", "Redirected", KIND_PERF),
    ConfigOwner("saturn", "Saturn", KIND_PERF),
    ConfigOwner("pluto", "Pluto", KIND_PERF),
    ConfigOwner("radium", "Radium", KIND_PERF),
    ConfigOwner("canary", "Canary", KIND_PERF),
    ConfigOwner("gputape", "GPUBooster", KIND_PERF, aliases=("gpubooster",)),
    ConfigOwner("smooth-boot", "Smooth Boot", KIND_PERF,
                aliases=("smoothbootfabric", "smoothbootreloaded",)),
    ConfigOwner("dashloader", "DashLoader", KIND_PERF),
    ConfigOwner("fastanim", "FastAnim", KIND_PERF,
                aliases=("fasterentityanimations",)),
    ConfigOwner("renderscale", "RenderScale", KIND_PERF),
    ConfigOwner("entity-view-distance", "Entity View Distance", KIND_PERF),
    ConfigOwner("fastchest", "FastChest", KIND_PERF),
    ConfigOwner("foamfix", "FoamFix", KIND_PERF),
    ConfigOwner("phosphor", "Phosphor", KIND_PERF),
    ConfigOwner("clumps", "Clumps", KIND_PERF, aliases=("lyclumps",)),
    ConfigOwner("stutterfix", "StutterFix", KIND_PERF),
    ConfigOwner("videotape", "Video Tape", KIND_PERF),
    ConfigOwner("better-biome-blend", "Better Biome Blend", KIND_PERF),
    ConfigOwner("bbrb", "Better Biome Reblend", KIND_PERF,
                aliases=("betterbiomereblend",)),
    ConfigOwner("performant", "Performant", KIND_PERF,
                note="1.12.2 时代的老牌优化模组"),
    ConfigOwner("betterfps", "BetterFps", KIND_PERF),
    ConfigOwner("vanillafix", "VanillaFix", KIND_PERF),
)


# ----------------------------------------------------------------------
# 规范化与匹配
# ----------------------------------------------------------------------
def _split(text: str) -> list[str]:
    """按非字母数字切词（`sodium-options` → ["sodium", "options"]）。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in str(text).lower():
        if ch.isalnum() and ch.isascii():
            buf.append(ch)
        elif buf:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


def normalize(text: str) -> str:
    """小写 + 去掉全部分隔符：`Dynamic_FPS` → `dynamicfps`。"""
    return "".join(_split(text))


# 配置文件名的通用尾巴：`sodium-options.json`、`clumps-common.toml` 之类。
# 剥掉这些词之后再比对模组 id（只认下面这几个，避免误伤别的模组）。
GENERIC_SUFFIXES = frozenset({
    "options", "option", "config", "configs", "settings", "setting",
    "client", "common", "server", "mixins", "mixin", "tweak", "tweaks",
    "optimization", "optimizations",
    # 附属模组的尾巴：sodium-extra.json / voxy-extra.json
    "extra", "extras",
    # 版本号尾巴：particle_core_config_v1.json / betterclouds-v1.json
    "v1", "v2", "v3", "v4",
})


def candidates(rel) -> list[str]:
    """
    从一条 config 相对路径推出可能的"模组 id"候选（按可信度排序）。

    `config/sodium-options.json`      → ["sodiumoptions", "sodium"]
    `config/sodium/x.json`            → ["sodium", "x"]
    `config/modernfix-mixins.properties` → ["modernfixmixins", "modernfix"]
    """
    path = Path(rel)
    parts = path.parts
    out: list[str] = []
    if len(parts) >= 3 and parts[0].lower() == "config":
        # config/<模组目录>/...
        out.append(normalize(parts[1]))
    if parts:
        tokens = _split(Path(parts[-1]).stem)
        out.append("".join(tokens))
        while len(tokens) > 1 and tokens[-1] in GENERIC_SUFFIXES:
            tokens.pop()
            out.append("".join(tokens))
    # 去空、去重，保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _build_index() -> dict[str, ConfigOwner]:
    index: dict[str, ConfigOwner] = {}
    for owner in TABLE:
        for raw in (owner.key,) + owner.aliases:
            index[normalize(raw)] = owner
    return index


_INDEX: dict[str, ConfigOwner] = _build_index()


def owner_of(rel) -> ConfigOwner | None:
    """这条 config 文件属于哪个"该保护"的模组；不属于返回 None。"""
    for cand in candidates(rel):
        owner = _INDEX.get(cand)
        if owner is not None:
            return owner
    return None


def is_protected(rel) -> bool:
    """这条 config 文件是否要"更新时不动玩家本地版本"。"""
    return owner_of(rel) is not None


def summarize(rels, limit: int = 3) -> str:
    """
    把一批被保护的文件汇总成一句人话：

        "Sodium（钠）、Iris Shaders（光影）等 6 个模组的 12 个配置"

    只用于日志/提示，不做逻辑判断。
    """
    names: list[str] = []
    count = 0
    for rel in rels:
        owner = owner_of(rel)
        if owner is None:
            continue
        count += 1
        if owner.name not in names:
            names.append(owner.name)
    if not count:
        return ""
    head = "、".join(names[:limit])
    if len(names) > limit:
        head += f" 等 {len(names)} 个模组"
    return f"{head} 的 {count} 个配置"

"""
test_config_exclude.py — 「智能剔除」专项测试
------------------------------------------------
覆盖四层：

  1. 归属表本身（规范、不重复、关键模组都在、别名可反查）
  2. 路径匹配（真实配置文件名命中；无关模组绝不命中）
  3. 判定层（apply_rules / build_plan / execute_plan / verify）
  4. 界面与下载任务（config 行四档、剔除的文件不下载）

运行：  python3 tests/test_config_exclude.py
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs  # noqa: E402  （让 UI 模块在无 GUI 环境可导入）

stubs.install()

from app.config import (  # noqa: E402
    CONFIG_STRATEGY_LIST,
    FILE_STRATEGY_LIST,
    STRATEGY_LIST,
    ChangeKind,
    Strategy,
    strategy_list_for,
)
from app.core import config_exclude as ce  # noqa: E402
from app.core.apply_rules import will_skip  # noqa: E402
from app.core.differ import Change  # noqa: E402
from app.core.updater import (  # noqa: E402
    PlanSource,
    SourceLayer,
    build_plan,
    execute_plan,
    split_smart_excluded,
    verify_after_update,
)


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Diff:
    """和 test_transfer 里同款的最小 DiffResult 替身。"""

    def __init__(self, added=(), modified=(), deleted=(),
                 folders_added=(), folders_modified=(), folders_deleted=()):
        self.added = ([Change(Path(r), ChangeKind.ADDED) for r in added]
                      + [Change(Path(r), ChangeKind.ADDED, is_folder_level=True)
                         for r in folders_added])
        self.modified = ([Change(Path(r), ChangeKind.MODIFIED) for r in modified]
                         + [Change(Path(r), ChangeKind.MODIFIED,
                                   is_folder_level=True)
                            for r in folders_modified])
        self.deleted = ([Change(Path(r), ChangeKind.DELETED) for r in deleted]
                        + [Change(Path(r), ChangeKind.DELETED,
                                  is_folder_level=True)
                           for r in folders_deleted])


# ======================================================================
# 1. 归属表本身
# ======================================================================
class TestTable(unittest.TestCase):
    def test_ids_are_plain_ascii(self):
        for owner in ce.TABLE:
            for raw in (owner.key,) + owner.aliases:
                self.assertTrue(raw, owner.name)
                self.assertEqual(raw, raw.lower(), f"id 含大写：{raw!r}")
                self.assertTrue(all(ch.isalnum() or ch in "-_." for ch in raw),
                                f"id 含奇怪字符：{raw!r}")

    def test_no_duplicate_ids(self):
        seen: dict[str, str] = {}
        for owner in ce.TABLE:
            for raw in (owner.key,) + owner.aliases:
                key = ce.normalize(raw)
                prev = seen.get(key)
                self.assertIn(prev, (None, owner.name),
                              f"{key!r} 同时属于 {prev} 和 {owner.name}")
                seen[key] = owner.name

    def test_every_entry_has_known_kind(self):
        for owner in ce.TABLE:
            self.assertIn(owner.kind, (ce.KIND_RENDER, ce.KIND_PERF),
                          f"{owner.name} 分类非法：{owner.kind}")
            self.assertTrue(owner.name.strip())

    def test_table_covers_the_essentials(self):
        """渲染器 + 主流客户端优化模组一个都不能少（防止误删条目）。"""
        must = [
            "sodium", "iris", "oculus", "optifine", "embeddium", "rubidium",
            "magnesium", "xenon", "vulkanmod", "canvas", "nothirium",
            "nvidium", "distanthorizons", "voxy", "sodium-extra",
            "immediatelyfast", "entityculling", "moreculling", "dynamic-fps",
            "badoptimizations", "ferrite-core", "krypton", "lithium",
            "modernfix", "memoryleakfix", "c2me", "ebe", "particle-core",
            "scalablelux", "starlight", "threadtweak", "cull-less-leaves",
            "fps-reducer", "exordium", "smooth-boot", "dashloader",
            "renderscale", "foamfix", "phosphor", "clumps", "saturn",
            "radium", "canary",
        ]
        keys = {ce.normalize(o.key) for o in ce.TABLE}
        missing = [m for m in must if ce.normalize(m) not in keys]
        self.assertEqual(missing, [], f"表里缺少这些模组：{missing}")

    def test_table_is_not_tiny(self):
        self.assertGreaterEqual(len(ce.TABLE), 60,
                                "归属表被误删了？至少应覆盖 60 个模组")

    def test_key_round_trip(self):
        for owner in ce.TABLE:
            got = ce.owner_of(f"config/{owner.key}.json")
            self.assertIs(got, owner, f"{owner.key} 无法反查到自己")

    def test_aliases_round_trip(self):
        for owner in ce.TABLE:
            for alias in owner.aliases:
                self.assertIs(ce.owner_of(f"config/{alias}.toml"), owner,
                              f"别名 {alias} 反查失败")

    def test_last_checked_format(self):
        self.assertRegex(ce.LAST_CHECKED, r"^\d{4}-\d{2}-\d{2}$")


# ======================================================================
# 2. 路径匹配
# ======================================================================
class TestMatching(unittest.TestCase):
    # 都是各模组**真实**的配置文件名（从 Modrinth 发行 jar 里核对过）
    REAL = {
        "config/sodium-options.json": "sodium",
        "config/iris.properties": "iris",
        "config/oculus.properties": "oculus",
        "config/embeddium-options.json": "embeddium",
        "config/rubidium-options.json": "rubidium",
        "config/xenon-options.json": "xenon",
        "config/magnesium-options.json": "magnesium",
        "config/DistantHorizons.toml": "distanthorizons",
        "config/sodium-extra-options.json": "sodium-extra",
        "config/immediatelyfast.json": "immediatelyfast",
        "config/entityculling.json": "entityculling",
        "config/moreculling.conf": "moreculling",
        "config/dynamic_fps.json": "dynamic-fps",
        "config/badoptimizations.txt": "badoptimizations",
        "config/modernfix-mixins.properties": "modernfix",
        "config/lithium.properties": "lithium",
        "config/scalablelux.properties": "scalablelux",
        "config/threadtweak.json": "threadtweak",
        "config/cull-less-leaves.json": "cull-less-leaves",
        "config/asyncparticles.json": "asyncparticles",
        "config/fpsreducer.conf": "fps-reducer",
        "config/exordium.json": "exordium",
        "config/saturn-optimizations.toml": "saturn",
        "config/smoothboot.json": "smooth-boot",
        "config/stutterfix-config.json": "stutterfix",
        "config/videotape.cfg": "videotape",
        "config/canvas.json5": "canvas",
        "config/nvidium-config.json": "nvidium",
        "config/flerovium.json": "flerovium",
        "config/particle_core_config_v1.json": "particle-core",
        "config/betterclouds-v1.json": "better-clouds",
        "config/BBEConfig.json": "better-block-entities",
        "config/bettergrass.json": "bettergrassify",
        "config/debugify.json": "debugify",
        "config/canary.properties": "canary",
        "config/vanillafix.cfg": "vanillafix",
        "config/foamfix.cfg": "foamfix",
        "config/entity-view-distance.json": "entity-view-distance",
        "config/obe.conf": "obe",
        "config/c2me.toml": "c2me",
        "config/vulkanmod.json": "vulkanmod",
        "config/voxy.json": "voxy",
        "config/voxy-extra.json": "voxy",
        "config/sodium/sodium-options.json": "sodium",
    }

    # 和优化/渲染毫无关系的模组：一个都不许命中
    UNRELATED = [
        "config/jei/jei.toml",
        "config/jei-common.toml",
        "config/create.toml",
        "config/create-client.toml",
        "config/ae2.json",
        "config/botania.cfg",
        "config/twilightforest.toml",
        "config/ponder.json",
        "config/jade.json",
        "config/curios.toml",
        "config/ars_nouveau.toml",
        "config/apotheosis.toml",
        "config/thermal.toml",
        "config/xaero/minimap.json",
        "config/fabric/indigo-renderer.properties",
        "config/forge-client.toml",
        "config/defaultoptions/extra.toml",
        "options.txt",
        "mods/sodium-fabric-0.5.11.jar",
        "resourcepacks/x.zip",
    ]

    def test_real_config_files_match(self):
        for rel, key in self.REAL.items():
            owner = ce.owner_of(rel)
            self.assertIsNotNone(owner, f"{rel} 应该被识别为优化/渲染配置")
            self.assertEqual(owner.key, key,
                             f"{rel} 认成了 {owner.key}，应为 {key}")

    def test_unrelated_configs_do_not_match(self):
        for rel in self.UNRELATED:
            self.assertIsNone(ce.owner_of(rel),
                              f"{rel} 不该被剔除（误伤普通模组）")

    def test_case_and_separators_are_ignored(self):
        for rel in ("config/DynamicFPS.json",
                    "config/dynamic-fps.json",
                    "config/dynamic_fps.json",
                    "config/DYNAMIC_FPS.JSON"):
            self.assertTrue(ce.is_protected(rel), rel)

    def test_generic_tails_are_stripped(self):
        cases = {
            "config/sodium-options.json": "sodium",
            "config/clumps-common.toml": "clumps",
            "config/fpsreducer-common.toml": "fps-reducer",
            "config/modernfix-mixins.properties": "modernfix",
            "config/stutterfix-config.json": "stutterfix",
            "config/particle_core_config_v1.json": "particle-core",
        }
        for rel, key in cases.items():
            self.assertEqual(ce.owner_of(rel).key, key, rel)

    def test_unknown_suffix_is_conservative(self):
        """没见过的尾巴宁可不动（少剔除 > 误剔除）。"""
        self.assertIsNone(ce.owner_of("config/sodium_totally_unknown.json"))

    def test_candidates_order(self):
        cands = ce.candidates("config/sodium-options.json")
        self.assertEqual(cands[0], "sodiumoptions")
        self.assertIn("sodium", cands)

    def test_weird_names_do_not_crash(self):
        """中文/空格/奇怪名字不能崩，也不能误命中。"""
        for rel in ("config/钠 优化.json", "config/我的配置.toml",
                    "config/a b/c d.json", "config/.hidden", "config/"):
            self.assertIsNone(ce.owner_of(rel), rel)
            self.assertFalse(ce.is_protected(rel), rel)

    def test_protected_inside_subdir(self):
        for rel in ("config/sodium/options.json",
                    "config/embeddium/embeddium-options.json",
                    "config/iris/shaders.properties"):
            self.assertTrue(ce.is_protected(rel), rel)

    def test_summarize(self):
        text = ce.summarize(["config/sodium-options.json",
                             "config/iris.properties",
                             "config/jei.toml"])
        self.assertIn("Sodium", text)
        self.assertIn("Iris", text)
        self.assertIn("2 个配置", text)
        self.assertNotIn("JEI", text)
        self.assertEqual(ce.summarize(["config/jei.toml"]), "")


# ======================================================================
# 3. 判定层
# ======================================================================
class TestRules(unittest.TestCase):
    def _skip(self, rel, kind=ChangeKind.MODIFIED, folder_level=False,
              strat=Strategy.SMART_EXCLUDE, top="config"):
        ch = Change(Path(rel), kind, is_folder_level=folder_level)
        return will_skip(ch, {top: True}, {top: strat})

    def test_protected_file_is_skipped(self):
        self.assertTrue(self._skip("config/sodium-options.json"))

    def test_other_config_is_applied(self):
        self.assertFalse(self._skip("config/jei.toml"))

    def test_added_protected_file_is_applied(self):
        """本地没有 → 照常写入（新装的优化模组也要有配置）。"""
        self.assertFalse(self._skip("config/sodium-options.json",
                                    kind=ChangeKind.ADDED))

    def test_deleted_protected_file_is_not_deleted(self):
        self.assertTrue(self._skip("config/sodium-options.json",
                                   kind=ChangeKind.DELETED))

    def test_folder_level_change_still_applies(self):
        """config 整体是文件夹级：不能整条灰掉，要进去逐个文件判。"""
        self.assertFalse(self._skip("config", folder_level=True))

    def test_other_strategies_unaffected(self):
        """替换重名 / 完全匹配照旧覆盖（智能剔除只是多出来的第四档）。"""
        for strat in (Strategy.REPLACE_SAME, Strategy.FULL_MATCH):
            self.assertFalse(
                self._skip("config/sodium-options.json", strat=strat),
                f"{strat} 下不该出现智能剔除行为")
        # 跳过重名本来就跳过一切，与智能剔除无关
        self.assertTrue(self._skip("config/jei.toml",
                                   strat=Strategy.SKIP_SAME))


class TestSplitSmartExcluded(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-ce-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_existing_files_are_skipped(self):
        _write(self.root / "config" / "sodium-options.json", "player")
        files = [Path("config/sodium-options.json"),
                 Path("config/iris.properties"),
                 Path("config/jei.toml")]
        kept, skipped = split_smart_excluded(files, self.root)
        self.assertEqual([p.as_posix() for p in kept],
                         ["config/iris.properties", "config/jei.toml"])
        self.assertEqual(skipped, ["config/sodium-options.json"])

    def test_without_old_root_everything_protected_is_skipped(self):
        files = [Path("config/sodium-options.json"),
                 Path("config/jei.toml")]
        kept, skipped = split_smart_excluded(files, None)
        self.assertEqual([p.as_posix() for p in kept], ["config/jei.toml"])
        self.assertEqual(skipped, ["config/sodium-options.json"])


# ======================================================================
# 4. 计划 + 执行（端到端）
# ======================================================================
class TestPlanAndExecute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-cex-")
        self.tmp = Path(self._tmp.name)
        self.inst = self.tmp / "instance"
        self.ov = self.tmp / "overrides"
        self.inst.mkdir(parents=True)
        self.ov.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _source(self):
        return PlanSource([SourceLayer(self.ov, consume=True)])

    def _plan(self, strat=Strategy.SMART_EXCLUDE, old_root=None):
        return build_plan(
            _Diff(folders_modified=["config"]),
            {"config": True}, {"config": strat},
            old_root=self.inst if old_root is None else old_root,
            source=self._source())

    def test_plan_records_skipped_files(self):
        _write(self.inst / "config" / "sodium-options.json", "player-tuned")
        _write(self.ov / "config" / "sodium-options.json", "pack-default")
        _write(self.ov / "config" / "iris.properties", "pack-iris")
        _write(self.ov / "config" / "jei.toml", "pack-jei")

        plan = self._plan()
        key = "config"
        self.assertEqual(plan.dir_skip[key], ["config/sodium-options.json"])
        # 被剔除的文件不进"期望值"（否则校验会报大小不符）
        self.assertNotIn("config/sodium-options.json", plan.dir_files[key])
        self.assertIn("config/iris.properties", plan.dir_files[key])
        self.assertIn("config/jei.toml", plan.dir_files[key])
        self.assertNotIn("config/sodium-options.json", plan.expect_size)

    def test_plan_keeps_new_protected_file(self):
        """本地没有这个配置 → 不在剔除名单里（要写进去）。"""
        _write(self.ov / "config" / "sodium-options.json", "pack-default")
        plan = self._plan()
        self.assertEqual(plan.dir_skip.get("config"), None)
        self.assertIn("config/sodium-options.json", plan.dir_files["config"])

    def test_plan_without_old_root_skips_all_protected(self):
        _write(self.ov / "config" / "sodium-options.json", "pack-default")
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True},
                          {"config": Strategy.SMART_EXCLUDE},
                          source=self._source())
        self.assertEqual(plan.dir_skip["config"],
                         ["config/sodium-options.json"])

    def test_other_strategies_have_no_skip_list(self):
        _write(self.inst / "config" / "sodium-options.json", "player")
        _write(self.ov / "config" / "sodium-options.json", "pack")
        plan = self._plan(strat=Strategy.REPLACE_SAME)
        self.assertEqual(plan.dir_skip, {})

    def test_execute_keeps_player_config_and_updates_rest(self):
        _write(self.inst / "config" / "sodium-options.json", "PLAYER-TUNED")
        _write(self.inst / "config" / "jei.toml", "old-jei")
        _write(self.ov / "config" / "sodium-options.json", "PACK-DEFAULT")
        _write(self.ov / "config" / "iris.properties", "pack-iris")
        _write(self.ov / "config" / "jei.toml", "new-jei")

        plan = self._plan()
        report = execute_plan(plan, self.inst, source=self._source())

        self.assertEqual(report["failed"], [])
        self.assertEqual(report["smart_skipped"], 1)
        self.assertEqual(
            (self.inst / "config" / "sodium-options.json").read_text(),
            "PLAYER-TUNED", "玩家的 Sodium 配置被覆盖了")
        self.assertEqual((self.inst / "config" / "jei.toml").read_text(),
                         "new-jei", "普通配置应当照常更新")
        self.assertEqual(
            (self.inst / "config" / "iris.properties").read_text(),
            "pack-iris")
        self.assertEqual(verify_after_update(plan, self.inst), [])

    def test_execute_writes_new_protected_config(self):
        """玩家没有的新配置照常落地（否则新装的模组没有默认值）。"""
        _write(self.ov / "config" / "sodium-options.json", "PACK-DEFAULT")
        plan = self._plan()
        report = execute_plan(plan, self.inst, source=self._source())
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["smart_skipped"], 0)
        self.assertEqual(
            (self.inst / "config" / "sodium-options.json").read_text(),
            "PACK-DEFAULT")

    def test_execute_still_merges_player_only_files(self):
        """智能剔除是"合并"，绝不能删玩家自己写的配置。"""
        _write(self.inst / "config" / "my-own.toml", "mine")
        _write(self.ov / "config" / "jei.toml", "pack-jei")
        plan = self._plan()
        execute_plan(plan, self.inst, source=self._source())
        self.assertTrue((self.inst / "config" / "my-own.toml").is_file())

    def test_replace_same_still_overwrites_protected_config(self):
        """不选智能剔除时行为不变：替换重名照旧覆盖。"""
        _write(self.inst / "config" / "sodium-options.json", "player")
        _write(self.ov / "config" / "sodium-options.json", "pack")
        plan = self._plan(strat=Strategy.REPLACE_SAME)
        report = execute_plan(plan, self.inst, source=self._source())
        self.assertEqual(report["smart_skipped"], 0)
        self.assertEqual(
            (self.inst / "config" / "sodium-options.json").read_text(),
            "pack")

    def test_full_match_still_replaces_whole_dir(self):
        _write(self.inst / "config" / "sodium-options.json", "player")
        _write(self.inst / "config" / "my-own.toml", "mine")
        _write(self.ov / "config" / "jei.toml", "pack-jei")
        plan = self._plan(strat=Strategy.FULL_MATCH)
        execute_plan(plan, self.inst, source=self._source())
        self.assertFalse((self.inst / "config" / "sodium-options.json").exists())
        self.assertFalse((self.inst / "config" / "my-own.toml").exists())

    def test_report_counts_add_up(self):
        _write(self.inst / "config" / "sodium-options.json", "player")
        _write(self.inst / "config" / "iris.properties", "player")
        _write(self.ov / "config" / "sodium-options.json", "pack")
        _write(self.ov / "config" / "iris.properties", "pack")
        _write(self.ov / "config" / "jei.toml", "pack")
        plan = self._plan()
        report = execute_plan(plan, self.inst, source=self._source())
        self.assertEqual(report["smart_skipped"], 2)
        self.assertEqual(report["failed"], [])


# ======================================================================
# 5. 策略列表 / 界面 / 下载任务
# ======================================================================
class TestDefaultStrategy(unittest.TestCase):
    """0.7.0 起 config 的默认策略就是「智能剔除」——这里守住它。"""

    def test_config_default_is_smart_exclude(self):
        from app.config import (DEFAULT_STRATEGY, DEFAULT_STRATEGY_BY_DIR,
                                ContentType)
        self.assertIs(DEFAULT_STRATEGY[ContentType.CONFIG],
                      Strategy.SMART_EXCLUDE)
        self.assertIs(DEFAULT_STRATEGY_BY_DIR["config"],
                      Strategy.SMART_EXCLUDE)

    def test_other_defaults_unchanged(self):
        from app.config import (DEFAULT_STRATEGY, DEFAULT_STRATEGY_BY_DIR,
                                ContentType, default_strategy_for_file)
        self.assertIs(DEFAULT_STRATEGY[ContentType.MODS], Strategy.FULL_MATCH)
        self.assertIs(DEFAULT_STRATEGY[ContentType.SAVES], Strategy.SKIP_SAME)
        self.assertIs(DEFAULT_STRATEGY[ContentType.OTHER],
                      Strategy.REPLACE_SAME)
        self.assertIs(DEFAULT_STRATEGY_BY_DIR["mods"], Strategy.FULL_MATCH)
        self.assertIs(DEFAULT_STRATEGY_BY_DIR["resourcepacks"],
                      Strategy.FULL_MATCH)
        # 根目录散文件仍是「保留键位」，不能被智能剔除带偏
        self.assertIs(default_strategy_for_file("options.txt"),
                      Strategy.MERGE_KEYS)

    def test_table_row_defaults_to_smart_exclude(self):
        from app.ui.widgets.strategy_table import StrategyTable
        table = StrategyTable(None)
        table.set_folders(["mods", "config", "saves"])
        got = table.get_strategies()
        self.assertEqual(got["config"], Strategy.SMART_EXCLUDE.value)
        self.assertEqual(got["mods"], Strategy.FULL_MATCH.value)
        self.assertEqual(got["saves"], Strategy.SKIP_SAME.value)
        # 按钮组选中态也要跟着默认值
        self.assertIs(table._rows["config"].group.get(),
                      Strategy.SMART_EXCLUDE)

    def test_player_default_for_update_pack(self):
        """玩家端（更新包）走 config 默认值；导出包仍是整包镜像。"""
        from app.core.pack_detector import PackKind
        from app.ui.player_view import PlayerView
        fake = types.SimpleNamespace(pack_kind=PackKind.UPDATE)
        got = PlayerView._default_strategy_for_top(
            fake, "config", False, PackKind.UPDATE)
        self.assertEqual(got, Strategy.SMART_EXCLUDE.value)
        got = PlayerView._default_strategy_for_top(
            fake, "config", False, PackKind.EXPORT)
        self.assertEqual(got, Strategy.FULL_MATCH.value)

    def test_plan_built_from_defaults_protects(self):
        """不改任何设置，直接按默认策略建计划：优化配置进 dir_skip。"""
        from app.config import default_strategy_for_dir
        tmp = tempfile.TemporaryDirectory(prefix="easier-ced-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst, ov = root / "inst", root / "ov"
        _write(inst / "config" / "sodium-options.json", "player")
        _write(ov / "config" / "sodium-options.json", "pack")
        _write(ov / "config" / "jei.toml", "pack")
        src = PlanSource([SourceLayer(ov, consume=True)])
        strategies = {"config": default_strategy_for_dir("config").value}
        plan = build_plan(_Diff(folders_modified=["config"]),
                          {"config": True}, strategies,
                          old_root=inst, source=src)
        self.assertEqual(plan.dir_skip["config"],
                         ["config/sodium-options.json"])
        report = execute_plan(plan, inst, source=src)
        self.assertEqual(report["smart_skipped"], 1)
        self.assertEqual((inst / "config" / "sodium-options.json").read_text(),
                         "player")
        self.assertEqual((inst / "config" / "jei.toml").read_text(), "pack")
        self.assertEqual(verify_after_update(plan, inst), [])


class TestStrategyList(unittest.TestCase):
    def test_config_has_four_options(self):
        self.assertEqual(strategy_list_for("config"),
                         CONFIG_STRATEGY_LIST)
        self.assertEqual(len(CONFIG_STRATEGY_LIST), 4)
        # 「智能剔除」既是默认值，也排在第一格（最常用的一档先看到）
        self.assertIs(CONFIG_STRATEGY_LIST[0], Strategy.SMART_EXCLUDE)
        self.assertEqual(CONFIG_STRATEGY_LIST[1:], STRATEGY_LIST)

    def test_other_dirs_keep_three(self):
        for top in ("mods", "saves", "resourcepacks", "custom"):
            self.assertEqual(strategy_list_for(top), STRATEGY_LIST, top)
            self.assertNotIn(Strategy.SMART_EXCLUDE,
                             strategy_list_for(top), top)

    def test_root_files_keep_merge_keys(self):
        got = strategy_list_for("options.txt", is_file=True)
        self.assertEqual(got, FILE_STRATEGY_LIST)
        self.assertIn(Strategy.MERGE_KEYS, got)
        self.assertNotIn(Strategy.SMART_EXCLUDE, got)

    def test_case_insensitive_dir_name(self):
        self.assertIn(Strategy.SMART_EXCLUDE, strategy_list_for("Config"))

    def test_strategy_value_parses(self):
        self.assertIs(Strategy("智能剔除"), Strategy.SMART_EXCLUDE)


class TestRealDiffIntegration(unittest.TestCase):
    """接真实 differ 跑一遍：config 是文件夹级变更，计划里要能接住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-cdi-")
        self.tmp = Path(self._tmp.name)
        self.old = self.tmp / "old"
        self.new = self.tmp / "new"
        for root in (self.old, self.new):
            (root / "mods").mkdir(parents=True)
        _write(self.old / "config" / "sodium-options.json", "PLAYER-TUNED")
        _write(self.old / "config" / "jei.toml", "old-jei")
        _write(self.new / "config" / "sodium-options.json", "PACK-DEFAULT")
        _write(self.new / "config" / "iris.properties", "pack-iris")
        _write(self.new / "config" / "jei.toml", "new-jei")

    def tearDown(self):
        self._tmp.cleanup()

    def test_diff_then_plan_then_execute(self):
        from app.core.differ import diff_packs_parallel
        diff = diff_packs_parallel(self.old, self.new)
        config_changes = [c for c in diff.modified
                          if c.rel_path.parts[0] == "config"]
        self.assertEqual(len(config_changes), 1)
        self.assertTrue(config_changes[0].is_folder_level,
                        "config 应当仍是文件夹级比对（不展开成几千个文件）")

        src = PlanSource([SourceLayer(self.new, consume=False)])
        plan = build_plan(diff, {"config": True},
                          {"config": Strategy.SMART_EXCLUDE},
                          old_root=self.old, source=src)
        self.assertEqual(plan.dir_skip["config"],
                         ["config/sodium-options.json"])
        report = execute_plan(plan, self.old, source=src)
        self.assertEqual(report["failed"], [])
        self.assertEqual(
            (self.old / "config" / "sodium-options.json").read_text(),
            "PLAYER-TUNED")
        self.assertEqual((self.old / "config" / "jei.toml").read_text(),
                         "new-jei")
        self.assertEqual((self.old / "config" / "iris.properties").read_text(),
                         "pack-iris")
        self.assertEqual(verify_after_update(plan, self.old), [])


class TestChangeListBadge(unittest.TestCase):
    """变更列表里 config 行要标出"智能剔除"（目录级没法逐条灰显）。"""

    def _rows(self, strategies):
        from app.ui.widgets.change_list import ChangeList
        cl = ChangeList(None)
        seen = []
        cl._add_group_row = lambda *a, **k: seen.append(
            {"top": a[1], "smart": k.get("smart", False)})
        cl.load(_Diff(folders_modified=["config", "mods"]),
                checked={"config": True, "mods": True},
                strategies=strategies)
        return {r["top"]: r["smart"] for r in seen}

    def test_badge_only_when_strategy_selected(self):
        rows = self._rows({"config": Strategy.SMART_EXCLUDE.value,
                           "mods": Strategy.FULL_MATCH.value})
        self.assertTrue(rows["config"])
        self.assertFalse(rows["mods"])

    def test_no_badge_for_other_strategies(self):
        rows = self._rows({"config": Strategy.REPLACE_SAME.value,
                           "mods": Strategy.FULL_MATCH.value})
        self.assertFalse(rows["config"])


class TestDownloadTasks(unittest.TestCase):
    """智能剔除掉的配置不该再被下载（反正不会写）。"""

    def _view(self, inst: Path):
        from app.ui.player_view import PlayerView
        fake = types.SimpleNamespace(
            pack_info=types.SimpleNamespace(path=inst),
            _smart_excluded=lambda rel: PlayerView._smart_excluded(fake, rel),
        )
        return fake, PlayerView._collect_download_tasks_from_diff

    def test_protected_existing_config_not_downloaded(self):
        tmp = tempfile.TemporaryDirectory(prefix="easier-cdt-")
        self.addCleanup(tmp.cleanup)
        inst = Path(tmp.name)
        _write(inst / "config" / "sodium-options.json", "player")
        merged = [
            {"path": "config/sodium-options.json", "downloads": ["http://x/1"]},
            {"path": "config/jei.toml", "downloads": ["http://x/2"]},
            {"path": "config/iris.properties", "downloads": ["http://x/3"]},
        ]
        fake, fn = self._view(inst)
        tasks = fn(fake, _Diff(folders_modified=["config"]), merged,
                   Path(tmp.name) / "ov", checked={"config": True},
                   strategies={"config": Strategy.SMART_EXCLUDE.value})
        rels = [t.rel_path for t in tasks]
        self.assertEqual(rels, ["config/jei.toml", "config/iris.properties"])

    def test_replace_same_downloads_everything(self):
        tmp = tempfile.TemporaryDirectory(prefix="easier-cdt-")
        self.addCleanup(tmp.cleanup)
        inst = Path(tmp.name)
        _write(inst / "config" / "sodium-options.json", "player")
        merged = [
            {"path": "config/sodium-options.json", "downloads": ["http://x/1"]},
        ]
        fake, fn = self._view(inst)
        tasks = fn(fake, _Diff(folders_modified=["config"]), merged,
                   Path(tmp.name) / "ov", checked={"config": True},
                   strategies={"config": Strategy.REPLACE_SAME.value})
        self.assertEqual([t.rel_path for t in tasks],
                         ["config/sodium-options.json"])


class TestAuditScript(unittest.TestCase):
    """scripts/config_exclude_list.py：列整张表 / 体检一个整合包。"""

    def _mod(self):
        import importlib.util
        path = _ROOT / "scripts" / "config_exclude_list.py"
        spec = importlib.util.spec_from_file_location("ce_list", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_table_text(self):
        text = self._mod()._table_text()
        self.assertIn(f"{len(ce.TABLE)} 个模组", text)
        self.assertIn("Sodium（钠）", text)
        self.assertIn(ce.KIND_RENDER, text)

    def test_audit_counts_and_groups(self):
        tmp = tempfile.TemporaryDirectory(prefix="easier-ces-")
        self.addCleanup(tmp.cleanup)
        pack = Path(tmp.name)
        for rel in ("config/sodium-options.json", "config/iris.properties",
                    "config/modernfix-mixins.properties",
                    "config/jei/jei.toml", "config/create-common.toml"):
            _write(pack / rel, "x")
        _write(pack / "mods" / "sodium-fabric-0.5.11.jar", "")
        data = self._mod()._audit(pack)
        self.assertEqual(data["config_files"], 5)
        self.assertEqual(data["protected_files"], 3)
        self.assertEqual(sorted(data["plain"]),
                         ["config/create-common.toml", "config/jei/jei.toml"])
        self.assertIn("Sodium（钠）", data["protected"])
        self.assertEqual(len(data["installed"]), 1)
        self.assertIn("sodium-fabric", data["installed"][0])
        self.assertIn("3 个会被", self._mod()._audit_text(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)

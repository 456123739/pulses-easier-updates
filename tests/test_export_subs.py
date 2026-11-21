r"""动态子项：存档 / 资源包 / 光影包能不能逐项"是否导出 + 更新策略"。

PCL 的语义是：这几个目录除了整目录开关，还会把里面每一项列成子选项，
逐项勾选是否导出；开发者端还要能逐项给"更新策略"。这里验证：

1. 子项能被扫描出来、默认勾选、计数正确；
2. 取消勾选 → 计数扣掉、规则里出现 `!目录\子项\`、真正不导出；
3. 子项策略单独落进 recommended_strategies；
4. zip 来源没有子项；
5. 重新扫描后子项状态不丢；
6. 玩家端按路径取策略（子项优先、回落父目录）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.config import Strategy
from app.core import database as db
from app.core import export_rules
from app.core.apply_rules import resolve_strategy_for
from app.ui_web.api import Api
from app.ui_web.controller import AppController
from app.ui_web.events import EventBus


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DevSubScopeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-subs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        dbdir = self.tmp / "db"
        db.create_database(dbdir)
        self._appcfg = mock.patch.object(db, "_APP_CFG", self.tmp / "app-config.json")
        self._appcfg.start()
        self.addCleanup(self._appcfg.stop)
        db.set_db_path(dbdir)

        self.pack = self.tmp / "versions" / "Pack"
        _write(self.pack / "mods" / "a.jar", "A")
        _write(self.pack / "config" / "c.toml", "c")
        _write(self.pack / "options.txt", "fov:90\n")
        _write(self.pack / "saves" / "WorldA" / "level.dat", "WA")
        _write(self.pack / "saves" / "WorldB" / "level.dat", "WB")
        _write(self.pack / "resourcepacks" / "p1.zip", "P1")
        _write(self.pack / "shaderpacks" / "s1.zip", "S1")

        self.zip = self.tmp / "Pack.mrpack"
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "name": "Pack",
                "versionId": "1.0.0", "files": [],
                "dependencies": {"minecraft": "1.20.1"}}))
            zf.writestr("overrides/mods/a.jar", "A")

        self.bus = EventBus()
        self.ctl = AppController(self.bus)
        self.api = Api(self.ctl, self.bus)
        self.ctl.set_role("developer")

    # ------------------------------------------------------------------
    def dev(self) -> dict:
        return self.api.invoke("dev.state", {})["data"]

    def wait_idle(self, timeout: float = 10.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            snap = self.dev()
            if snap["phase"] != "loading":
                return snap
            time.sleep(0.02)
        self.fail(f"识别没结束：{snap}")

    def open(self, path) -> dict:
        res = self.api.invoke("dev.open", {"path": str(path)})
        self.assertTrue(res["ok"], res)
        return self.wait_idle()

    def row(self, snap: dict, name: str) -> dict:
        return next(r for r in snap["strategies"] if r["name"] == name)

    def sub(self, snap: dict, folder: str, name: str) -> dict:
        return next(s for s in self.row(snap, folder).get("subs", [])
                    if s["name"] == name)

    # ------------------------------------------------------------------
    def test_subs_listed_with_counts_and_default_checked(self):
        snap = self.open(self.pack)
        snap = self.api.invoke("dev.strategy.set",
                               {"name": "saves", "excluded": False})["data"]
        saves = self.row(snap, "saves")
        self.assertEqual(saves.get("title"), "存档")
        names = [s["name"] for s in saves["subs"]]
        self.assertEqual(names, ["WorldA", "WorldB"])
        self.assertTrue(all(s["checked"] for s in saves["subs"]))
        self.assertTrue(all(s["kind"] == "dir" for s in saves["subs"]))
        self.assertEqual(self.sub(snap, "saves", "WorldA")["files"], 1)
        self.assertEqual(self.sub(snap, "saves", "WorldA")["strategy"], "")

        packs = self.row(snap, "resourcepacks")
        self.assertEqual([s["name"] for s in packs["subs"]], ["p1.zip"])
        self.assertEqual(packs["subs"][0]["kind"], "file")
        self.assertEqual(packs["subs"][0]["files"], 1)
        # 不在子项范围的目录不能带 subs
        self.assertNotIn("subs", self.row(snap, "mods"))

    def test_uncheck_sub_drops_counts_and_rules(self):
        self.open(self.pack)
        self.api.invoke("dev.strategy.set", {"name": "saves", "excluded": False})
        before = self.dev()
        total_before = before["instance"]["totals"]["files"]

        snap = self.api.invoke("dev.sub.set",
                               {"path": "saves/WorldA", "checked": False})["data"]
        self.assertEqual(snap["instance"]["totals"]["files"], total_before - 1)
        self.assertFalse(self.sub(snap, "saves", "WorldA")["checked"])
        # 另一个存档照旧
        self.assertTrue(self.sub(snap, "saves", "WorldB")["checked"])

        rules = [r.lower() for r in self.ctl.dev._current_rules()]
        self.assertIn("saves\\*", rules)
        self.assertIn("!saves\\worlda\\*", rules)
        # 排除规则必须排在包含规则之后，否则会被包含规则盖掉
        self.assertGreater(rules.index("!saves\\worlda\\*"),
                           rules.index("saves\\*"))
        self.assertFalse(export_rules.should_keep("saves/WorldA/level.dat", rules))
        self.assertTrue(export_rules.should_keep("saves/WorldB/level.dat", rules))

        # 勾回来：计数恢复，规则消失
        snap = self.api.invoke("dev.sub.set",
                               {"path": "saves/WorldA", "checked": True})["data"]
        self.assertEqual(snap["instance"]["totals"]["files"], total_before)
        self.assertNotIn("!saves\\worlda\\*",
                         [r.lower() for r in self.ctl.dev._current_rules()])

    def test_sub_strategy_goes_to_recommended_strategies(self):
        self.open(self.pack)
        snap = self.api.invoke("dev.sub.set", {
            "path": "resourcepacks/p1.zip", "strategy": "跳过重名"})["data"]
        item = self.sub(snap, "resourcepacks", "p1.zip")
        self.assertEqual(item["strategy"], "跳过重名")
        self.assertEqual(item["inherited"], "完全匹配")

        rec = self.ctl.dev._recommended_strategies()
        self.assertEqual(rec["resourcepacks/p1.zip"], "跳过重名")

        # 空串 = 跟随父目录，不再写进包里
        self.api.invoke("dev.sub.set",
                        {"path": "resourcepacks/p1.zip", "strategy": ""})
        self.assertNotIn("resourcepacks/p1.zip",
                         self.ctl.dev._recommended_strategies())

    def test_bad_inputs_are_rejected(self):
        self.open(self.pack)
        res = self.api.invoke("dev.sub.set",
                              {"path": "saves/NotThere", "checked": False})
        self.assertFalse(res["ok"])
        res = self.api.invoke("dev.sub.set", {
            "path": "resourcepacks/p1.zip", "strategy": "乱写"})
        self.assertFalse(res["ok"])

    def test_saves_row_is_visible_but_excluded_by_default(self):
        snap = self.open(self.pack)
        saves = self.row(snap, "saves")
        self.assertTrue(saves["excluded"])
        self.assertEqual(saves.get("subs"), [])
        # 点一下目录名（纳入）→ 重扫后子项出现
        snap = self.api.invoke("dev.strategy.set",
                               {"name": "saves", "excluded": False})["data"]
        self.assertFalse(self.row(snap, "saves")["excluded"])
        self.assertEqual([x["name"] for x in self.row(snap, "saves")["subs"]],
                         ["WorldA", "WorldB"])

    def test_sub_all_toggles_folder(self):
        self.open(self.pack)
        snap = self.api.invoke("dev.sub.all",
                               {"folder": "resourcepacks", "checked": False})["data"]
        self.assertFalse(self.sub(snap, "resourcepacks", "p1.zip")["checked"])
        snap = self.api.invoke("dev.sub.all",
                               {"folder": "resourcepacks", "checked": True})["data"]
        self.assertTrue(self.sub(snap, "resourcepacks", "p1.zip")["checked"])
        res = self.api.invoke("dev.sub.all",
                              {"folder": "mods", "checked": False})
        self.assertFalse(res["ok"])

    def test_zip_mode_has_no_subs(self):
        snap = self.open(self.zip)
        self.assertEqual(snap["mode"], "zip")
        for row in snap["strategies"]:
            self.assertNotIn("subs", row)

    def test_sub_state_survives_rescan(self):
        self.open(self.pack)
        self.api.invoke("dev.strategy.set", {"name": "saves", "excluded": False})
        self.api.invoke("dev.sub.set", {"path": "saves/WorldA", "checked": False})
        self.api.invoke("dev.sub.set", {
            "path": "saves/WorldA", "strategy": "替换重名"})
        # 触发一次重扫（打开/关掉一个规则）
        snap = self.api.invoke("dev.rule.set",
                               {"key": "screenshots", "value": True})["data"]
        item = self.sub(snap, "saves", "WorldA")
        self.assertFalse(item["checked"])
        self.assertEqual(item["strategy"], "替换重名")
        self.assertEqual(item["files"], 1)

    def test_clear_drops_subs(self):
        self.open(self.pack)
        self.api.invoke("dev.strategy.set", {"name": "saves", "excluded": False})
        snap = self.api.invoke("dev.clear", {})["data"]
        self.assertEqual(self.ctl.dev.subs, {})
        self.assertEqual(snap["strategies"], [])


class ResolveStrategyForCase(unittest.TestCase):
    def test_sub_key_wins_over_top(self):
        strategies = {"resourcepacks": "完全匹配",
                      "resourcepacks/p1.zip": "跳过重名"}
        self.assertEqual(resolve_strategy_for(strategies, "resourcepacks/p1.zip"),
                         Strategy.SKIP_SAME)
        self.assertEqual(resolve_strategy_for(strategies, "resourcepacks/p2.zip"),
                         Strategy.FULL_MATCH)

    def test_dir_key_matches_nested_file(self):
        strategies = {"saves/WorldA": "跳过重名", "saves": "完全匹配"}
        self.assertEqual(
            resolve_strategy_for(strategies, "saves/WorldA/level.dat"),
            Strategy.SKIP_SAME)
        self.assertEqual(
            resolve_strategy_for(strategies, "saves/WorldB/level.dat"),
            Strategy.FULL_MATCH)

    def test_unknown_falls_back_to_full_match(self):
        self.assertEqual(resolve_strategy_for({}, "saves/WorldA/level.dat"),
                         Strategy.FULL_MATCH)
        self.assertEqual(resolve_strategy_for({"saves": "乱写"}, "saves/x"),
                         Strategy.FULL_MATCH)


if __name__ == "__main__":
    unittest.main()

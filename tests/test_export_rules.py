"""导出规则引擎：逐条对齐 PCL 的 Like / 顺序覆盖 / 目录补星。"""

import unittest

from app.core import export_rules as R


class LikeCase(unittest.TestCase):
    def test_star(self):
        self.assertTrue(R.like_to_regex("mods/*.jar").match("mods/a.jar"))
        self.assertTrue(R.like_to_regex("mods/*.jar").match("mods/sub/b.jar"))
        self.assertFalse(R.like_to_regex("mods/*.jar").match("mods/a.zip"))

    def test_question_and_digit(self):
        self.assertTrue(R.like_to_regex("a?c").match("abc"))
        self.assertFalse(R.like_to_regex("a?c").match("ac"))
        self.assertTrue(R.like_to_regex("v#").match("v7"))
        self.assertFalse(R.like_to_regex("v#").match("vx"))

    def test_char_class(self):
        pat = R.like_to_regex("[abc].txt")
        self.assertTrue(pat.match("a.txt"))
        self.assertFalse(pat.match("d.txt"))
        neg = R.like_to_regex("[!abc].txt")
        self.assertFalse(neg.match("a.txt"))
        self.assertTrue(neg.match("d.txt"))

    def test_special_chars_are_literal(self):
        self.assertTrue(R.like_to_regex("config/x.toml").match("config/x.toml"))
        self.assertFalse(R.like_to_regex("config/x.toml").match("config/xXtoml"))

    def test_case_insensitive(self):
        self.assertTrue(R.like_to_regex("MODS/*.JAR").match("mods/a.jar"))


class StandardizeCase(unittest.TestCase):
    def test_skips_comments_and_equals(self):
        lines = R.standardize_lines(["", "# 注释", "=k=v", "mods/"])
        self.assertEqual(lines, ["mods\\*"])

    def test_slash_to_backslash_and_suffix(self):
        self.assertEqual(R.standardize_lines(["a/b/"]), ["a\\b\\*"])
        self.assertEqual(R.standardize_lines(["a/b/"], add_suffix_star=False),
                         ["a\\b\\"])

    def test_keeps_negation(self):
        self.assertEqual(R.standardize_lines(["!x/*.log"]), ["!x\\*.log"])


class ShouldKeepCase(unittest.TestCase):
    def rules(self, *lines):
        return R.standardize_lines(list(lines))

    def test_last_rule_wins(self):
        r = self.rules("mods/", "!mods/*.disabled")
        self.assertTrue(R.should_keep("mods/a.jar", r))
        self.assertFalse(R.should_keep("mods/a.disabled", r))

    def test_later_include_overrides_earlier_exclude(self):
        r = self.rules("!mods/", "mods/keep.jar")
        self.assertTrue(R.should_keep("mods/keep.jar", r))
        self.assertFalse(R.should_keep("mods/other.jar", r))

    def test_dot_disabled_default(self):
        r = R.expand_rules()
        self.assertTrue(R.should_keep("mods/a.jar", r))
        self.assertFalse(R.should_keep("mods/a.jar.disabled", r))
        self.assertFalse(R.should_keep("mods/a.jar.old", r))

    def test_dot_disabled_included_when_selected(self):
        r = R.expand_rules({"mods", "mods-disabled"})
        self.assertTrue(R.should_keep("mods/a.jar.disabled", r))

    def test_fixed_excludes_always_last(self):
        r = R.expand_rules()
        self.assertFalse(R.should_keep("logs/latest.log", r))
        self.assertFalse(R.should_keep("saves/x.dat_old", r))

    def test_defaults_include_options_and_exclude_screenshots(self):
        r = R.expand_rules()
        self.assertTrue(R.should_keep("options.txt", r))
        self.assertFalse(R.should_keep("screenshots/a.png", r))

    def test_saves_only_when_selected(self):
        self.assertFalse(R.should_keep("saves/world/level.dat", R.expand_rules()))
        r = R.expand_rules({"saves"})
        self.assertTrue(R.should_keep("saves/world/level.dat", r))

    def test_nested_config_not_leaking_player_data(self):
        r = R.expand_rules()
        self.assertTrue(R.should_keep("config/mod.toml", r))
        self.assertFalse(R.should_keep("config/jei/world/bookmarks.ini", r))


if __name__ == "__main__":
    unittest.main()

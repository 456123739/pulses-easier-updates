"""
test_mirrors.py — 镜像层（app/core/mirrors.py）
================================================
只测"派生 + 排序 + 健康度"这三件纯逻辑，不碰真实网络：

  1. Modrinth 官方地址会被派生到镜像前面；其它主机原样不动
  2. 关掉 mirror_first 或清空 mirror_hosts → 原样返回（行为与旧版一致）
  3. 两种镜像路径变体（<host><path> / <host>/modrinth<path>）
  4. 健康度：同一镜像连续失败 3 次 → 本轮不再出现；成功一次立刻复活
  5. 顺序稳定、去重

运行：  python3 tests/test_mirrors.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs

CDN = ("https://cdn.modrinth.com/data/AANobbMI/versions/g6ju16VJ/"
       "sodium-fabric-0.9.3.jar")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-mir-")
        cls._restore = stubs.isolate_user_dirs(cls._tmp.name)
        from app.core import database as db
        cls.db = db
        cls.dbdir = Path(cls._tmp.name) / "db"
        db.create_database(cls.dbdir)
        db.set_db_path(cls.dbdir)

    @classmethod
    def tearDownClass(cls):
        cls._restore()
        cls._tmp.cleanup()

    def setUp(self):
        from app.core import mirrors
        self.m = mirrors
        mirrors.reset()
        self.db.set_download_options({
            "mirror_first": 1,
            "mirror_hosts": "https://mirror.example",
        })

    def tearDown(self):
        self.m.reset()


class TestOrdered(_Base):
    def test_mirror_first_for_modrinth(self):
        out = self.m.ordered([CDN])
        self.assertEqual(len(out), 3, out)
        self.assertTrue(out[0].startswith("https://mirror.example/"),
                        "镜像必须排在第一个")
        self.assertIn("/data/AANobbMI/versions/g6ju16VJ/", out[0])
        self.assertEqual(out[-1], CDN, "官方地址必须留在最后兜底")

    def test_two_path_variants(self):
        out = self.m.candidates(CDN)
        self.assertTrue(any(u == "https://mirror.example" + CDN.split(
            "cdn.modrinth.com")[1] for u in out))
        self.assertTrue(any(u.startswith("https://mirror.example/modrinth/")
                            for u in out))

    def test_other_hosts_untouched(self):
        url = "https://example.com/a.jar"
        self.assertEqual(self.m.candidates(url), [])
        self.assertEqual(self.m.ordered([url]), [url])

    def test_disabled_returns_same_object(self):
        self.db.set_download_options({"mirror_first": 0})
        urls = [CDN]
        self.assertIs(self.m.ordered(urls), urls)

    def test_empty_hosts_returns_same_object(self):
        self.db.set_download_options({"mirror_hosts": ""})
        urls = [CDN]
        self.assertIs(self.m.ordered(urls), urls)

    def test_dedup_and_order(self):
        out = self.m.ordered([CDN, CDN])
        self.assertEqual(len(out), len(set(out)), "不能有重复项")
        self.assertEqual(out.count(CDN), 1)

    def test_multiple_hosts_keep_order(self):
        self.db.set_download_options({
            "mirror_first": 1,
            "mirror_hosts": "https://a.example;https://b.example"})
        out = self.m.ordered([CDN])
        a = [u for u in out if u.startswith("https://a.example")]
        b = [u for u in out if u.startswith("https://b.example")]
        self.assertTrue(a and b)
        self.assertLess(out.index(a[0]), out.index(b[0]),
                        "按配置顺序排")


class TestHealth(_Base):
    def test_three_failures_disable_host(self):
        for _ in range(3):
            self.m.note_failure("https://mirror.example" + CDN.split(
                "cdn.modrinth.com")[1])
        self.assertIn("https://mirror.example", self.m.dead_hosts())
        out = self.m.ordered([CDN])
        self.assertEqual(out, [CDN], "挂掉的镜像不该再出现")

    def test_success_revives(self):
        for _ in range(3):
            self.m.note_failure("https://mirror.example/x")
        self.assertIn("https://mirror.example", self.m.dead_hosts())
        self.m.note_success("https://mirror.example/x")
        self.assertNotIn("https://mirror.example", self.m.dead_hosts())

    def test_unknown_host_not_tracked(self):
        self.m.note_failure("https://other.example/x")
        self.assertEqual(self.m.dead_hosts(), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)

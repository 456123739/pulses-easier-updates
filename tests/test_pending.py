"""
test_pending.py — 「更新受阻 / 补齐缺口」回归测试
================================================
覆盖：

  纯逻辑（app/core/pending.py）
    * 文件名归一化 / 候选匹配（浏览器副本后缀、大小写）
    * 链接整理 / 源名称
    * 凭证读写：json + !!!更新未完成-请阅读!!!.txt（原子写）
    * check_marker：文件已补齐 → resolved；否则 outstanding
  界面状态机（PendingWindow，桩环境）
    * 分页：◀/▶、滚轮、点击跳目录、绕回
    * 每页独立拖入框（懒创建）
    * 跨页拖入自动切页
    * 校验失败保留该页
    * 全部补齐 → on_all_resolved 只触发一次
    * 重新下载：运行中按钮禁用；成功关页、失败保留
    * 收起 ≠ 放弃；destroy 取消所有挂起 after
  整合行为
    * player_view：放弃 → 写凭证 + 清续传记录
    * 定位整合包 → 探测凭证（缺失→询问、已齐→静默清理）
    * 补齐模式：跑只含缺失文件的补丁计划 → 清理凭证
    * 凭证文件永不参与更新比对
"""

import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-pend-")
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
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=self._tmp.name))


# ======================================================================
# 纯逻辑
# ======================================================================
class TestPendingLogic(_Base):
    def test_entry_from_task_and_source_rows(self):
        from app.core import pending as P
        task = types.SimpleNamespace(
            rel_path="mods/a.jar",
            urls=["https://cdn.modrinth.com/a.jar",
                  "https://mod.mcimirror.top/a.jar"],
            sha1="a" * 40, sha256="", sha512="", file_size=123)
        item = P.entry_from_task(task)
        self.assertEqual(item["rel"], "mods/a.jar")
        self.assertEqual(item["sha1"], "a" * 40)
        self.assertEqual(item["size"], 123)
        self.assertEqual([n for n, _u in P.source_rows(item["urls"])],
                         ["Modrinth 官方", "MCIM 镜像"])

    def test_file_matches_hash_and_size(self):
        from app.core import pending as P
        f = self.tmp / "x.bin"
        f.write_bytes(b"hello")
        sha1 = hashlib.sha1(b"hello").hexdigest()
        self.assertTrue(P.file_matches(f, {"sha1": sha1, "size": 5}))
        self.assertFalse(P.file_matches(f, {"sha1": "0" * 40, "size": 5}))
        self.assertFalse(P.file_matches(f, {"sha1": sha1, "size": 9}))
        self.assertFalse(P.file_matches(self.tmp / "nope", {"size": 5}))

    def test_entries_from_tasks_skips_ready(self):
        from app.core import pending as P
        cache = self.tmp / "cache"
        (cache / "mods").mkdir(parents=True)
        good = b"GOOD"
        (cache / "mods" / "ok.jar").write_bytes(good)
        sha1 = hashlib.sha1(good).hexdigest()
        tasks = [types.SimpleNamespace(rel_path="mods/ok.jar", urls=["u"],
                                       sha1=sha1, sha256="", sha512="",
                                       file_size=len(good)),
                 types.SimpleNamespace(rel_path="mods/miss.jar", urls=["u"],
                                       sha1="0" * 40, sha256="", sha512="",
                                       file_size=10)]
        out = P.entries_from_tasks(tasks, cache, {"mods/miss.jar": "超时"})
        self.assertEqual([e["rel"] for e in out], ["mods/miss.jar"])
        self.assertEqual(out[0]["error"], "超时")


class TestMarker(_Base):
    def _marker(self, pack_root: Path, rel="mods/a.jar", sha1="a" * 40,
                size=3):
        from app.core import pending as P
        return P.build_marker(
            pack_root, pack_name="测试包", pack_version="2.0",
            update_zip="/tmp/up.eapack", update_fingerprint="fp",
            cache_root="/tmp/cache",
            missing=[{"rel": rel, "sha1": sha1, "size": size,
                      "urls": ["https://cdn.modrinth.com/a.jar"],
                      "error": "超时"}])

    def test_write_read_clear(self):
        from app.core import pending as P
        pack = self.tmp / "instance"
        pack.mkdir()
        marker = self._marker(pack)
        self.assertTrue(P.write_marker(pack, marker))
        self.assertTrue(P.marker_path(pack).is_file())
        self.assertTrue(P.notice_path(pack).is_file())
        self.assertEqual(P.notice_path(pack).name, P.NOTICE_FILENAME)
        self.assertIn("!!!", P.NOTICE_FILENAME)
        text = P.notice_path(pack).read_text("utf-8")
        self.assertIn("更新未完成", text)
        self.assertIn("mods/a.jar", text)
        self.assertIn("Modrinth 官方", text)
        self.assertIn("应放到", text)

        got = P.read_marker(pack)
        self.assertEqual(got["pack_name"], "测试包")
        self.assertEqual(got["missing"][0]["rel"], "mods/a.jar")

        self.assertTrue(P.clear_marker(pack))
        self.assertFalse(P.marker_path(pack).is_file())
        self.assertFalse(P.notice_path(pack).is_file())
        self.assertFalse(P.marker_dir(pack).exists(),
                         "凭证目录空了就该删掉")

    def test_check_marker_outstanding_then_resolved(self):
        from app.core import pending as P
        pack = self.tmp / "inst2"
        pack.mkdir()
        data = b"DATA"
        sha1 = hashlib.sha1(data).hexdigest()
        P.write_marker(pack, self._marker(pack, sha1=sha1, size=len(data)))

        res = P.check_marker(pack)
        self.assertTrue(res["has"])
        self.assertFalse(res["resolved"])
        self.assertEqual(len(res["outstanding"]), 1)

        # 玩家自己把文件放好了 → 下次定位应当判为"已补齐"
        (pack / "mods").mkdir(parents=True)
        (pack / "mods" / "a.jar").write_bytes(data)
        res2 = P.check_marker(pack)
        self.assertTrue(res2["resolved"])
        self.assertEqual(res2["outstanding"], [])

    def test_no_marker(self):
        from app.core import pending as P
        pack = self.tmp / "inst3"
        pack.mkdir()
        self.assertFalse(P.check_marker(pack)["has"])
        self.assertFalse(P.has_local_only(pack))


class TestSources(unittest.TestCase):
    def test_source_display_name(self):
        from app.core.downloader import describe_sources, source_display_name
        cases = {
            "https://cdn.modrinth.com/data/x/1/a.jar": "Modrinth 官方",
            "https://mediafilez.forgecdn.net/files/1/2/a.jar": "CurseForge 官方",
            "https://mod.mcimirror.top/data/x/1/a.jar": "MCIM 镜像",
            "https://example.com/a.jar": "example.com",
        }
        for url, want in cases.items():
            self.assertEqual(source_display_name(url), want, url)
        self.assertEqual(
            [n for n, _u in describe_sources(
                ["https://cdn.modrinth.com/a", "https://cdn.modrinth.com/a",
                 "https://mod.mcimirror.top/b"])],
            ["Modrinth 官方", "MCIM 镜像"])


# ======================================================================
# 子窗口状态机
# ======================================================================
class TestPendingWindow(_Base):
    def _win(self, items, drop=None, retry=None, all_resolved=None,
             give_up=None, dismiss=None, hide=None, mode="wait"):
        from app.ui.widgets.pending_window import PendingWindow
        win = PendingWindow(
            None, mode=mode, items=items,
            on_file_dropped=drop or (lambda rel, path: True),
            on_retry=retry,
            on_all_resolved=all_resolved,
            on_give_up=give_up,
            on_dismiss=dismiss,
            on_hide=hide,
            on_notice=lambda lv, t: None)

        def _sync_after(_ms, fn=None, *a, **k):
            if callable(fn):
                fn(*a, **k)
            return "after#sync"

        win.after = _sync_after
        return win

    @staticmethod
    def _item(rel, urls=("u",), error="超时"):
        return {"rel": rel, "urls": list(urls), "error": error}

    def test_paging_wraps(self):
        win = self._win([self._item("mods/a.jar"),
                         self._item("mods/b.jar"),
                         self._item("mods/c.jar")])
        self.assertEqual(win.current_rel(), "mods/a.jar")
        win._step(1)
        self.assertEqual(win.current_rel(), "mods/b.jar")
        win._step(-1)
        self.assertEqual(win.current_rel(), "mods/a.jar")
        win._step(-1)
        self.assertEqual(win.current_rel(), "mods/c.jar", "应当绕回最后一页")
        win._step(1)
        self.assertEqual(win.current_rel(), "mods/a.jar")

    def test_wheel_switches_page(self):
        win = self._win([self._item("mods/a.jar"), self._item("mods/b.jar")])
        down = types.SimpleNamespace(delta=-120, num=0)
        up = types.SimpleNamespace(delta=120, num=0)
        win._on_wheel(down)
        self.assertEqual(win.current_rel(), "mods/b.jar")
        win._on_wheel(up)
        self.assertEqual(win.current_rel(), "mods/a.jar")

    def test_goto_by_click(self):
        win = self._win([self._item("mods/a.jar"), self._item("mods/b.jar")])
        win._goto("mods/b.jar")
        self.assertEqual(win.current_rel(), "mods/b.jar")

    def test_drop_uses_this_page_and_keeps_page_on_verify_failure(self):
        seen = []
        win = self._win([self._item("mods/a.jar")],
                        drop=lambda rel, path: seen.append(rel) or False)
        win._handle_drop("mods/a.jar", [Path("/tmp/a.jar")])
        self.assertEqual(seen, ["mods/a.jar"])
        self.assertTrue(win.has_pending(), "校验不过要保留这一页")

    def test_per_page_drop_zone_lazy_and_destroyed_on_resolve(self):
        win = self._win([self._item("mods/a.jar"),
                         self._item("mods/b.jar")])
        win._refresh_page()
        self.assertIn("mods/a.jar", win._drops)
        win._goto("mods/b.jar")
        self.assertIn("mods/b.jar", win._drops)
        win.resolve("mods/b.jar")
        self.assertNotIn("mods/b.jar", win._drops)
        self.assertEqual(win.pending_rels(), ["mods/a.jar"])

    def test_all_resolved_fires_once(self):
        fired = []
        win = self._win([self._item("mods/a.jar")],
                        all_resolved=lambda: fired.append(1))
        win.resolve("mods/a.jar")
        self.assertEqual(fired, [1])
        win.resolve("mods/a.jar")          # 再来一次不该重复触发
        self.assertEqual(fired, [1])

    def test_retry_flow(self):
        states = []

        def retry(rel, report):
            states.append((rel, "start"))
            report("ok")

        win = self._win([self._item("mods/a.jar")], retry=retry)
        win._on_retry_click()
        self.assertEqual(states[0], ("mods/a.jar", "start"))
        self.assertFalse(win.has_pending(), "重新下载成功应当关掉这一页")

    def test_retry_failure_keeps_page_and_reports(self):
        win = self._win([self._item("mods/a.jar")],
                        retry=lambda rel, report: report("failed", "还是超时"))
        win._on_retry_click()
        self.assertTrue(win.has_pending())
        self.assertEqual(win._items["mods/a.jar"]["error"], "还是超时")

    def test_retry_button_disabled_while_running(self):
        win = self._win([self._item("mods/a.jar")],
                        retry=lambda rel, report: None)
        win.report_retry("mods/a.jar", "running")
        self.assertIn("mods/a.jar", win._retrying)
        win.report_retry("mods/a.jar", "failed", "x")
        self.assertNotIn("mods/a.jar", win._retrying)

    def test_hide_is_not_give_up(self):
        seen = []
        win = self._win([self._item("mods/a.jar")],
                        give_up=lambda: seen.append("give_up"),
                        hide=lambda: seen.append("hide"))
        win.hide_window()
        self.assertEqual(seen, ["hide"])
        self.assertEqual(win.pending_count(), 1, "收起后待补入项必须还在")

    def test_give_up_and_dismiss_callbacks(self):
        seen = []
        win = self._win([self._item("mods/a.jar")],
                        give_up=lambda: seen.append("give_up"))
        win.alt_btn._command() if hasattr(win.alt_btn, "_command") else None
        win._fire(win.on_give_up)()
        self.assertEqual(seen, ["give_up"])

        seen2 = []
        win2 = self._win([self._item("mods/a.jar")], mode="complete",
                         dismiss=lambda: seen2.append("dismiss"))
        win2._fire(win2.on_dismiss)()
        self.assertEqual(seen2, ["dismiss"])

    def test_destroy_cancels_after_ids(self):
        win = self._win([self._item("mods/a.jar")])
        win._after_ids = ["after#1", "after#2"]
        cancelled = []
        win.after_cancel = lambda i: cancelled.append(i)
        win.destroy()
        self.assertEqual(sorted(cancelled), ["after#1", "after#2"])
        self.assertEqual(win._after_ids, [])
        self.assertTrue(win._closed)

    def test_has_tall_layout_and_warning_style(self):
        src = (_ROOT / "app" / "ui" / "widgets"
               / "pending_window.py").read_text(encoding="utf-8")
        self.assertIn("_WIN_W, _WIN_H = 500, 720", src)
        self.assertIn("Color.WARNING", src)
        self.assertIn("self.transient(", src)
        self.assertNotIn(".grab_set(", src, "补入窗口不能模态阻塞主窗口")
        self.assertNotIn(".wait_window(", src, "不能嵌套事件循环")
        self.assertIn("_DROP_H = 150", src)


# ======================================================================
# 与 player_view 的整合
# ======================================================================
class TestPlayerPendingIntegration(_Base):
    def _view(self):
        import main as main_mod
        from app.ui.main_window import MainWindow
        AppBase, _dnd = main_mod._pick_app_base()
        app = type("W", (MainWindow, AppBase), {})()
        return app, app.player_view

    def _pack(self) -> Path:
        z = self.tmp / "pack.eapack"
        with __import__("zipfile").ZipFile(z, "w") as zf:
            zf.writestr("modrinth.index.json",
                        json.dumps({"files": [], "name": "P"}))
            zf.writestr("ea_settings.json", "{}")
            zf.writestr("ea_manifest.json", "{}")
            zf.writestr("overrides/", b"")
        return z

    def test_give_up_writes_marker_and_clears_resume(self):
        from app.core import pending as P
        _app, pv = self._view()

        pack = self._pack()
        inst = self.tmp / "inst"
        (inst / "mods").mkdir(parents=True)
        pv.pack_info = types.SimpleNamespace(name="P", version="1", path=inst)
        pv.update_zip = pack
        pv._cache_root = self.tmp / "cache"
        pv._cache_root.mkdir()
        pv._download_tasks = [types.SimpleNamespace(
            rel_path="mods/a.jar", urls=["https://cdn.modrinth.com/a.jar"],
            sha1="a" * 40, sha256="", sha512="", file_size=10)]
        pv._pending_errors = {"mods/a.jar": "超时"}
        # 先落一份续传记录，验证放弃后会清掉
        pv._write_resume()
        self.assertTrue((pv._cache_root / "resume.json").is_file())

        pv._give_up = True
        pv._give_up_missing = pv._compute_missing()
        pv._on_apply_done({"applied": 1, "deleted": 0, "skipped": 0,
                           "failed": []}, [])
        self.assertTrue(P.marker_path(inst).is_file())
        self.assertTrue(P.notice_path(inst).is_file())
        self.assertFalse((pv._cache_root / "resume.json").is_file())
        self.assertFalse(pv._give_up)
        marker = P.read_marker(inst)
        self.assertEqual(marker["missing"][0]["rel"], "mods/a.jar")
        self.assertEqual(marker["cache_root"], str(pv._cache_root))

    def test_compute_missing_skips_files_already_correct(self):
        _app, pv = self._view()

        inst = self.tmp / "inst2"
        (inst / "mods").mkdir(parents=True)
        data = b"OK"
        (inst / "mods" / "a.jar").write_bytes(data)
        pv.pack_info = types.SimpleNamespace(name="P", version="1", path=inst)
        pv._cache_root = self.tmp / "cache2"
        pv._cache_root.mkdir()
        pv._download_tasks = [
            types.SimpleNamespace(
                rel_path="mods/a.jar", urls=["u"],
                sha1=hashlib.sha1(data).hexdigest(), sha256="", sha512="",
                file_size=len(data)),
            types.SimpleNamespace(
                rel_path="mods/b.jar", urls=["u"], sha1="0" * 40,
                sha256="", sha512="", file_size=10)]
        pv._pending_errors = {"mods/a.jar": "x", "mods/b.jar": "y"}
        missing = pv._compute_missing()
        self.assertEqual([m["rel"] for m in missing], ["mods/b.jar"])

    def test_check_pending_record_asks_when_missing(self):
        from app.core import pending as P
        _app, pv = self._view()

        inst = self.tmp / "inst3"
        (inst / "mods").mkdir(parents=True)
        P.write_marker(inst, P.build_marker(
            inst, pack_name="X", missing=[
                {"rel": "mods/a.jar", "sha1": "0" * 40, "size": 1,
                 "urls": ["u"]}]))
        pv.pack_info = types.SimpleNamespace(name="X", version="1", path=inst)
        asked = []
        with mock.patch.object(pv, "winfo_toplevel", return_value=None), \
                mock.patch("app.ui.player_view.confirm",
                           side_effect=lambda *a, **k: asked.append(k)):
            pv._check_pending_record()
        self.assertEqual(len(asked), 1, "有缺口时必须询问")
        self.assertIn("现在补齐", asked[0]["confirm_text"])

    def test_check_pending_record_cleans_when_resolved(self):
        from app.core import pending as P
        _app, pv = self._view()

        inst = self.tmp / "inst4"
        (inst / "mods").mkdir(parents=True)
        data = b"OK"
        (inst / "mods" / "a.jar").write_bytes(data)
        P.write_marker(inst, P.build_marker(
            inst, pack_name="X", missing=[
                {"rel": "mods/a.jar",
                 "sha1": hashlib.sha1(data).hexdigest(),
                 "size": len(data), "urls": ["u"]}]))
        pv.pack_info = types.SimpleNamespace(name="X", version="1", path=inst)
        with mock.patch.object(pv, "winfo_toplevel", return_value=None), \
                mock.patch("app.ui.player_view.confirm") as cf:
            pv._check_pending_record()
        cf.assert_not_called()
        self.assertFalse(P.marker_path(inst).is_file())
        self.assertFalse(P.notice_path(inst).is_file())

    def test_enter_complete_mode_builds_pages(self):
        from app.core import pending as P
        _app, pv = self._view()

        inst = self.tmp / "inst5"
        (inst / "mods").mkdir(parents=True)
        cache = self.tmp / "cache5"
        marker = P.build_marker(inst, pack_name="X", cache_root=str(cache),
                                missing=[{"rel": "mods/a.jar",
                                          "sha1": "0" * 40, "size": 1,
                                          "urls": ["u"]}])
        pv.pack_info = types.SimpleNamespace(name="X", version="1", path=inst)
        pv._enter_complete_mode(marker, marker["missing"])
        self.assertEqual(pv._pending_mode, "complete")
        self.assertTrue(cache.is_dir(), "缓存目录不存在时要按凭证重建")
        self.assertTrue(pv.pending_banner is not None)
        self.assertEqual(pv._record_rels, ["mods/a.jar"])
        self.assertEqual([i["rel"] for i in pv._pending_items()],
                         ["mods/a.jar"])

    def test_finish_complete_mode_runs_patch_plan(self):
        from app.core import pending as P
        _app, pv = self._view()

        inst = self.tmp / "inst6"
        (inst / "mods").mkdir(parents=True)
        cache = self.tmp / "cache6" / "mods"
        cache.mkdir(parents=True)
        data = b"REAL"
        sha1 = hashlib.sha1(data).hexdigest()
        (cache / "a.jar").write_bytes(data)
        marker = P.build_marker(inst, pack_name="X",
                                cache_root=str(cache.parent),
                                missing=[{"rel": "mods/a.jar", "sha1": sha1,
                                          "size": len(data), "urls": ["u"]}])
        P.write_marker(inst, marker)
        pv.pack_info = types.SimpleNamespace(name="X", version="1", path=inst)
        pv._enter_complete_mode(marker, marker["missing"])

        done = []
        with mock.patch.object(pv, "after",
                               side_effect=lambda ms, fn=None, *a, **k:
                               fn(*a, **k) if callable(fn) else None):
            pv._finish_complete_mode()
            import time
            for _ in range(100):
                if pv._record_rels == [] or pv._phase == pv._PHASE_IDLE:
                    break
                time.sleep(0.05)
            done.append(pv._phase)
        self.assertTrue((inst / "mods" / "a.jar").is_file(),
                        "补齐的文件应当被复制到整合包")
        self.assertEqual((inst / "mods" / "a.jar").read_bytes(), data)
        self.assertFalse(P.marker_path(inst).is_file(),
                         "补齐成功后应当清理凭证")
        self.assertFalse(P.notice_path(inst).is_file())

    def test_marker_files_never_appear_as_changes(self):
        """凭证文件是本地专属，永不能被当成更新内容或被删除。"""
        from app.core import pending as P
        from app.core.differ import diff_packs_parallel
        old = self.tmp / "old"
        new = self.tmp / "new"
        (old / "mods").mkdir(parents=True)
        (new / "mods").mkdir(parents=True)
        (old / "mods" / "a.jar").write_bytes(b"x")
        P.write_marker(old, P.build_marker(
            old, missing=[{"rel": "mods/missing.jar", "sha1": "0" * 40,
                           "size": 1, "urls": ["u"]}]))
        diff = diff_packs_parallel(old, new, whitelist=["mods"],
                                   index_hashes={})
        rels = {c.rel_path.as_posix()
                for c in diff.added + diff.modified + diff.deleted}
        self.assertNotIn(P.NOTICE_FILENAME, rels)
        self.assertNotIn(P.MARKER_DIR, rels)

    def test_no_skip_button_anywhere(self):
        """「全部跳过」以及"带缺失静默完成"的旧路径必须彻底消失。"""
        for rel in ("app/ui/player_view.py",
                    "app/ui/widgets/pending_window.py",
                    "app/ui/widgets/pending_banner.py"):
            src = (_ROOT / rel).read_text(encoding="utf-8")
            bad = [w for w in ("全部跳过", "_on_skip_all") if w in src]
            self.assertEqual(bad, [], f"{rel} 里还留着旧跳过路径：{bad}")
        pv = (_ROOT / "app" / "ui" / "player_view.py").read_text(
            encoding="utf-8")
        bad = [w for w in ("_apply_after_abort", "_skip_after_abort",
                           "全部跳过") if w in pv]
        self.assertEqual(bad, [], f"player_view 里还留着旧跳过路径：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

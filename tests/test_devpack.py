"""
test_devpack.py — 第二批（闭环）回归测试
------------------------------------------------
覆盖：

  P1-1  应用中断可发现（检查点）+ 校验不只查存在
  P1-2  resume 记录在更新包失效时**不被删除**，「重新定位」路径可达
  P1-8  _completed_files 由 on_file_done 回填
  P1-11 index 声明的任意路径都能下载/应用（不再被白名单过滤丢弃）
  P1-13 缓存目录带指纹；应用只并入本次任务清单
  P1-14 成功后清 resume、失败时文案不再说"完成"
  P1-17 开发者端：解析失败清状态、解析期加锁、解压不在主线程、黑名单首次生效
  P1-18 导出原子写、index 复制失败视为致命
  P1-19 min_size 对 index 条目真正生效
  P1-20 tamper_proof 有真正的校验点
  安全：index / overrides 里的非法相对路径被丢弃

运行：  python3 tests/test_devpack.py
"""

import hashlib
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                     # noqa: E402


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_src_pack(root: Path, *, overrides: bool = True,
                   excluded_dir: str | None = None) -> Path:
    """造一个最小源包 ZIP（启动器导出的整合包）"""
    root.mkdir(parents=True, exist_ok=True)
    files = [
        {"path": "mods/a.jar", "hashes": {"sha1": "a" * 40},
         "downloads": ["https://example.invalid/a.jar"], "fileSize": 10},
        {"path": "shaderpacks/s.zip", "hashes": {"sha1": "b" * 40},
         "downloads": ["https://example.invalid/s.zip"], "fileSize": 20},
    ]
    if excluded_dir:
        files.append(
            {"path": f"{excluded_dir}/big.bin",
             "hashes": {"sha1": "c" * 40},
             "downloads": ["https://example.invalid/big.bin"],
             "fileSize": 999})
    index = {"formatVersion": 1, "game": "minecraft", "versionId": "9.9.9",
             "name": "RoundTrip", "dependencies": {"minecraft": "1.20.1"},
             "files": files}
    (root / "modrinth.index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    if overrides:
        _write(root / "overrides" / "config" / "c.toml", "key = 1\n")
        _write(root / "overrides" / "mods" / "local.jar", "LOCAL")
        if excluded_dir:
            _write(root / f"overrides/{excluded_dir}/o.bin", "O")
    zip_path = root.parent / (root.name + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in root.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(root).as_posix())
    return zip_path


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "customtkinter" not in sys.modules:
            stubs.install()
        else:
            stubs.install()
        cls._tmp = tempfile.TemporaryDirectory(prefix="easier-dp-")
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
        self.tmp = Path(tempfile.mkdtemp(prefix="case-",
                                         dir=self._tmp.name))


# ======================================================================
# 导出 / 解析 往返
# ======================================================================
class TestRoundTrip(_Base):
    def test_export_parse_roundtrip(self):
        from app.core import eapack as ep
        from app.core.mrpack import merge_files, parse_mrpack
        from app.core.pack_detector import PackKind, detect_pack_kind

        src = self.tmp / "src"
        src_zip = _make_src_pack(src)
        pack, err = parse_mrpack(src_zip)
        self.assertFalse(err)
        out = self.tmp / "out.eapack"
        res = ep.export_eapack(
            source_dir=src, source_zip=src_zip, out_path=out, pack=pack,
            merged_files=merge_files(pack),
            strategies={"mods": "完全匹配", "config": "替换重名"},
            changelog_md="# 更新\n- 内容\n", version="9.9.9")
        self.assertIsNotNone(res)
        self.assertTrue(out.is_file())

        kind, kerr = detect_pack_kind(out)
        self.assertEqual(kind, PackKind.UPDATE, kerr)

        # 读回各种元数据
        settings = ep.read_settings(out)
        self.assertEqual(settings["recommended_strategies"]["config"],
                         "替换重名")
        self.assertEqual(ep.read_whitelist(out),
                         self.db.get_whitelist())
        self.assertIn("更新", ep.read_changelog(out) or "")
        self.assertEqual(ep.read_recommended_strategies(out)["mods"],
                         "完全匹配")
        manifest = ep.read_manifest(out)
        self.assertEqual(manifest["pack_version"], "9.9.9")
        self.assertTrue(manifest["has_changelog"])

        # 导出包本身还能被解析回 MRPack
        pack2, err2 = parse_mrpack(out)
        self.assertFalse(err2)
        self.assertEqual(pack2.name, "RoundTrip")
        merged = merge_files(pack2)
        paths = {i["path"] for i in merged}
        self.assertIn("mods/a.jar", paths)
        self.assertIn("config/c.toml", paths)
        self.assertIn("mods/local.jar", paths)

    def test_index_entries_outside_merge_folders_survive(self):
        """P1-11：index 里 shaderpacks/ 的条目不能被丢掉。"""
        from app.core.mrpack import merge_files, parse_mrpack
        src = self.tmp / "src2"
        zip_path = _make_src_pack(src)
        pack, _ = parse_mrpack(zip_path)
        paths = {i["path"] for i in merge_files(pack)}
        self.assertIn("shaderpacks/s.zip", paths)

    def test_unsafe_paths_are_dropped(self):
        from app.core.mrpack import MRFile, MRPack, merge_files
        pack = MRPack(name="x", files=[
            MRFile(path="../../evil.txt", downloads=["u"]),
            MRFile(path="/abs.txt", downloads=["u"]),
            MRFile(path="C:/win.txt", downloads=["u"]),
            MRFile(path="mods/ok.jar", downloads=["u"]),
        ])
        pack.overrides_files = ["../esc.js", "kubejs/ok.js"]
        paths = {i["path"] for i in merge_files(pack)}
        self.assertEqual(paths, {"mods/ok.jar", "kubejs/ok.js"})

    def test_nested_source_pack_keeps_overrides(self):
        """
        源包把 index 与 overrides 放在一层子目录里时，导出**不能丢掉
        overrides**（旧实现只认根级 overrides/，会导出成空内容残包）。
        """
        from app.core import eapack as ep
        from app.core.mrpack import merge_files, parse_mrpack
        root = self.tmp / "nested_src" / "MyPack"
        _write(root / "modrinth.index.json",
               json.dumps({"formatVersion": 1, "name": "N",
                           "versionId": "1", "files": []}))
        _write(root / "overrides" / "config" / "a.toml", "k = 1\n")
        zip_path = self.tmp / "nested_src.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for p in (self.tmp / "nested_src").rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(self.tmp
                                              / "nested_src").as_posix())
        pack, err = parse_mrpack(zip_path)
        self.assertFalse(err)
        extract = self.tmp / "nested_extract"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract)
        out = self.tmp / "nested.eapack"
        res = ep.export_eapack(
            source_dir=extract, source_zip=zip_path, out_path=out,
            pack=pack, merged_files=merge_files(pack), strategies={},
            changelog_md="")
        self.assertIsNotNone(res)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        self.assertIn("overrides/config/a.toml", names)

    def test_no_overrides_still_creates_overrides_entry(self):
        from app.core import eapack as ep
        from app.core.mrpack import OVERRIDES_DIR, merge_files, parse_mrpack
        src = self.tmp / "src3"
        zip_path = _make_src_pack(src, overrides=False)
        pack, _ = parse_mrpack(zip_path)
        out = self.tmp / "no_ovr.eapack"
        self.assertIsNotNone(ep.export_eapack(
            source_dir=src, source_zip=zip_path, out_path=out, pack=pack,
            merged_files=merge_files(pack), strategies={}, changelog_md=""))
        with zipfile.ZipFile(out) as zf:
            self.assertIn(OVERRIDES_DIR + "/", zf.namelist())


# ======================================================================
# min_size / 完整性 / 原子写
# ======================================================================
class TestExportOptions(_Base):
    def _export(self, name, src, zip_path, pack, **kw):
        from app.core import eapack as ep
        from app.core.mrpack import merge_files
        out = self.tmp / name
        return ep.export_eapack(
            source_dir=src, source_zip=zip_path, out_path=out, pack=pack,
            merged_files=merge_files(pack), strategies={},
            changelog_md="# log", **kw), out

    def test_min_size_filters_index_and_overrides(self):
        from app.core.mrpack import parse_mrpack
        src = self.tmp / "src4"
        zip_path = _make_src_pack(src, excluded_dir="resourcepacks")
        pack, _ = parse_mrpack(zip_path)
        self.db.set_export_options({"min_size": True})
        try:
            res, out = self._export("min.eapack", src, zip_path, pack,
                                    excluded_folders=["resourcepacks"])
        finally:
            self.db.set_export_options({"min_size": False})
        self.assertIsNotNone(res)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            index = json.loads(zf.read("modrinth.index.json").decode())
        self.assertNotIn("overrides/resourcepacks/o.bin", names)
        self.assertFalse(
            [f for f in index["files"]
             if f["path"].startswith("resourcepacks/")],
            "最小体积模式下 index 里仍留有被排除目录的条目")
        self.assertTrue(
            [f for f in index["files"] if f["path"] == "mods/a.jar"])

    def test_signature_verifies_and_detects_tampering(self):
        from app.core import eapack as ep
        from app.core.mrpack import parse_mrpack
        src = self.tmp / "src5"
        zip_path = _make_src_pack(src)
        pack, _ = parse_mrpack(zip_path)
        self.db.set_export_options({"tamper_proof": True})
        try:
            res, out = self._export("signed.eapack", src, zip_path, pack)
        finally:
            self.db.set_export_options({"tamper_proof": False})
        self.assertIsNotNone(res)
        ok, reason = ep.verify_signature(out)
        self.assertTrue(ok, reason)
        manifest = ep.read_manifest(out)
        self.assertTrue(manifest.get("signature"))

        # 篡改：改掉 changelog 的内容后重打包（保持其余条目不变）
        tampered = self.tmp / "tampered.eapack"
        with zipfile.ZipFile(out) as src_zf:
            items = [(i, src_zf.read(i.filename))
                     for i in src_zf.infolist()]
        with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as dst_zf:
            for info, data in items:
                if info.filename == ep.CHANGELOG_FILE:
                    data = b"# HACKED"
                dst_zf.writestr(info, data)
        ok2, reason2 = ep.verify_signature(tampered)
        self.assertFalse(ok2, "篡改后签名仍然通过")
        self.assertIn("完整性", reason2)

    def test_unsigned_pack_is_reported_as_unsigned(self):
        from app.core import eapack as ep
        from app.core.mrpack import parse_mrpack
        src = self.tmp / "src6"
        zip_path = _make_src_pack(src)
        pack, _ = parse_mrpack(zip_path)
        self.db.set_export_options({"tamper_proof": False})
        res, out = self._export("unsigned.eapack", src, zip_path, pack)
        self.assertIsNotNone(res)
        ok, reason = ep.verify_signature(out)
        self.assertTrue(ok)
        self.assertEqual(reason, "unsigned")

    def test_index_copy_failure_is_fatal_and_leaves_no_partial_file(self):
        from app.core import eapack as ep
        from app.core.mrpack import MRPack
        src = self.tmp / "src7"
        src.mkdir(parents=True)
        _write(src / "overrides" / "x.txt", "x")
        bad_zip = self.tmp / "no_index.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("hello.txt", "hi")
        out = self.tmp / "broken.eapack"
        res = ep.export_eapack(source_dir=src, source_zip=bad_zip,
                               out_path=out, pack=MRPack(name="x"),
                               merged_files=[], strategies={},
                               changelog_md="")
        self.assertIsNone(res)
        self.assertFalse(out.exists(), "失败却留下半成品 .eapack")
        self.assertFalse((self.tmp / "broken.eapack.part-eapack").exists(),
                         "失败却留下临时文件")

    def test_precompute_overrides_writes_hashes(self):
        from app.core import eapack as ep
        from app.core.mrpack import parse_mrpack
        src = self.tmp / "src8"
        zip_path = _make_src_pack(src)
        pack, _ = parse_mrpack(zip_path)
        self.db.set_export_options({"include_hashes": False,
                                    "precompute_overrides": True})
        try:
            res, out = self._export("pre.eapack", src, zip_path, pack)
        finally:
            self.db.set_export_options({"include_hashes": True,
                                        "precompute_overrides": True})
        self.assertIsNotNone(res)
        hashes = ep.read_hashes(out)
        self.assertIsNotNone(hashes)
        # 白名单目录走文件级 → 有逐个文件的哈希
        self.assertIn("mods/local.jar", hashes["files"])
        # 非白名单目录走文件夹级 → 有顶层目录哈希（玩家端据此省一次读取）
        self.assertIn("config", hashes["folders"])


# ======================================================================
# blake2b 期望哈希 + 目录哈希消费
# ======================================================================
class TestRecordedHashes(_Base):
    def test_blake2b_expected_hash_used(self):
        from app.core import hashing
        from app.core.differ import diff_packs_parallel
        old = self.tmp / "old"
        new = self.tmp / "new"
        _write(old / "config" / "a.toml", "key = 1\n")
        _write(new / "config" / "a.toml", "key = 1\n")
        algo, value = hashing.file_hash(new / "config" / "a.toml")
        self.assertEqual(algo, "blake2b")
        # 不传期望哈希 → 内容相同（两侧都算）→ 无 MODIFIED
        d0 = diff_packs_parallel(old, new, whitelist=["config"])
        self.assertEqual(d0.modified, [])
        # 传错哈希 → 必须报 MODIFIED（说明 blake2b 通道真的被用上了）
        d1 = diff_packs_parallel(old, new, whitelist=["config"],
                                 index_hashes={"config/a.toml":
                                               {"blake2b": "0" * 32}})
        self.assertEqual(len(d1.modified), 1)

    def test_recorded_folder_hash_short_circuits(self):
        from app.core.differ import folder_hash, diff_packs_parallel
        old = self.tmp / "old2"
        new = self.tmp / "new2"
        _write(old / "kubejs" / "a.js", "same")
        _write(new / "kubejs" / "a.js", "same")
        recorded = folder_hash(new / "kubejs", content=True)
        d = diff_packs_parallel(old, new, whitelist=["mods"],
                                folder_hashes={"kubejs": recorded})
        self.assertEqual(d.modified, [])
        d2 = diff_packs_parallel(old, new, whitelist=["mods"],
                                 folder_hashes={"kubejs": "f" * 64})
        self.assertEqual(len(d2.modified), 1)


# ======================================================================
# 校验 / 检查点
# ======================================================================
class TestVerifyAndCheckpoint(_Base):
    def test_verify_detects_size_mismatch_and_leftovers(self):
        from app.core.updater import UpdatePlan, verify_after_update
        old = self.tmp / "inst"
        new = self.tmp / "src"
        _write(new / "mods" / "a.jar", "LONGER CONTENT")
        _write(old / "mods" / "a.jar", "short")
        _write(old / "mods" / "should-be-gone.jar", "x")
        plan = UpdatePlan(copy=[Path("mods/a.jar")],
                          delete=[Path("mods/should-be-gone.jar")])
        failed = verify_after_update(plan, old, new)
        rels = {f["rel"] for f in failed}
        self.assertIn("mods/a.jar", rels)
        self.assertIn("mods/should-be-gone.jar", rels)

    def test_verify_passes_after_real_apply(self):
        from app.core.updater import (UpdatePlan, execute_plan,
                                      verify_after_update)
        old = self.tmp / "inst2"
        new = self.tmp / "src2"
        _write(new / "mods" / "a.jar", "CONTENT")
        _write(new / "config" / "c.toml", "new")
        _write(old / "mods" / "old.jar", "old")
        old.mkdir(parents=True, exist_ok=True)
        plan = UpdatePlan(copy=[Path("mods/a.jar")],
                          replace_dirs=[(Path("config"),
                                         __import__("app.config",
                                                    fromlist=["Strategy"])
                                         .Strategy.REPLACE_SAME)])
        report = execute_plan(plan, old, new)
        self.assertEqual(report["failed"], [])
        self.assertEqual(verify_after_update(plan, old, new), [])

    def test_execute_plan_stops_at_verify_until_checked(self):
        from app.core import checkpoint as cp_mod
        from app.config import Strategy
        from app.core.updater import UpdatePlan, execute_plan
        old = self.tmp / "inst3"
        new = self.tmp / "src3"
        _write(new / "mods" / "a.jar", "CONTENT")
        plan = UpdatePlan(copy=[Path("mods/a.jar")])
        execute_plan(plan, old, new)
        # 先直接读文件确认写成了 applied（scan_checkpoints 会顺手清掉它）
        import json
        from app.core.checkpoint import _pack_hash
        path = (self.dbdir / "cache" / "checkpoints"
                / f"{_pack_hash(old)}.json")
        # execute_plan 只负责"搬运完成"→ stage=verify（校验是调用方的事）
        # 这样"搬运完了但校验途中被杀"下次启动能发现
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text("utf-8"))["stage"],
                         "verify")
        # 停在 verify = "搬运完了但收尾没做完"：必须被当成中断上报
        mid = [c for c in cp_mod.scan_checkpoints()
               if c.pack_root == str(old)]
        self.assertEqual(len(mid), 1)
        self.assertEqual(mid[0].stage, "verify")
        # 调用方校验通过后写 applied → 才算"正常结束"
        cp_mod.mark_applied(old)
        self.assertEqual(json.loads(path.read_text("utf-8"))["stage"],
                         "applied")
        self.assertEqual([c for c in cp_mod.scan_checkpoints()
                          if c.pack_root == str(old)], [])
        self.assertFalse(path.is_file())


    def test_interrupted_checkpoint_is_detected(self):
        from app.core import checkpoint as cp_mod
        old = self.tmp / "inst4"
        cp_mod.save_checkpoint(cp_mod.Checkpoint(
            pack_root=str(old), stage="copy",
            done=["mods/a.jar"], remaining=["mods/b.jar"]))
        found = [c for c in cp_mod.scan_checkpoints()
                 if c.pack_root == str(old)]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].stage, "copy")
        # 提示过之后清掉，不会每次启动都弹
        cp_mod.clear_checkpoint(old)
        self.assertEqual([c for c in cp_mod.scan_checkpoints()
                          if c.pack_root == str(old)], [])


# ======================================================================
# resume 重定位
# ======================================================================
class TestResumeRelocate(_Base):
    def test_invalid_zip_record_is_kept(self):
        from app.core import resume as rs
        cache = self.dbdir / "cache" / "update_packs" / "p1"
        zip_path = self.tmp / "pack.zip"
        zip_path.write_bytes(b"x" * 10)
        rs.write_resume(cache, zip_path, self.tmp / "inst",
                        completed=["mods/a.jar"], failed=[],
                        total=1, started_at=1.0, pack_name="P")
        zip_path.unlink()          # 更新包被移走
        records = rs.scan_all_resumes(self.dbdir)
        self.assertEqual(len(records), 1, "失效记录被静默删除了（重定位不可达）")
        avail = rs.check_resume_availability(records[0])
        self.assertFalse(avail["zip_ok"])
        self.assertTrue(avail["zip_reason"])

    def test_relocate_zip_restores_availability(self):
        from app.core import resume as rs
        cache = self.dbdir / "cache" / "update_packs" / "p2"
        pack = self.tmp / "moved.zip"
        pack.write_bytes(b"y" * 20)
        rs.write_resume(cache, pack, self.tmp / "inst",
                        completed=[], failed=[], total=0, started_at=1.0)
        moved = self.tmp / "moved-away.zip"
        pack.rename(moved)
        data = rs.read_resume(cache)
        self.assertFalse(rs.check_resume_availability(data)["zip_ok"])
        rs.write_resume(cache, moved, self.tmp / "inst", completed=[],
                        failed=[], total=0, started_at=1.0)
        data2 = rs.read_resume(cache)
        avail = rs.check_resume_availability(data2)
        self.assertTrue(avail["zip_ok"], avail["zip_reason"])

    def test_stale_part_cleanup_only_touches_part_files(self):
        from app.core import resume as rs
        cache = self.tmp / "cache"
        _write(cache / "mods" / "a.jar.part", "p")
        _write(cache / "mods" / "a.jar.part.meta", "m")
        _write(cache / "mods" / "part-of-mod.jar", "keep me")
        n = rs.cleanup_stale_parts(cache)
        self.assertEqual(n, 2)
        self.assertTrue((cache / "mods" / "part-of-mod.jar").is_file())


# ======================================================================
# 开发者端行为
# ======================================================================
class TestDeveloperView(_Base):
    def _view(self):
        from app.ui.developer_view import DeveloperView
        return DeveloperView(None)

    def test_parse_error_clears_state(self):
        view = self._view()
        from app.core.mrpack import MRPack
        view.pack = MRPack(name="stale")
        view.merged = [{"path": "mods/old.jar"}]
        view.source_zip = Path("/tmp/old.zip")
        view._parsing = True
        view._on_parse_error("坏了")
        self.assertIsNone(view.pack)
        self.assertEqual(view.merged, [])
        self.assertIsNone(view.source_zip)
        self.assertFalse(view._parsing)

    def test_drop_rejected_while_parsing(self):
        view = self._view()
        view._parsing = True
        view.source_zip = Path("/tmp/keep.zip")
        view._on_pack_dropped([Path("/tmp/new.zip")])
        self.assertEqual(view.source_zip, Path("/tmp/keep.zip"))

    def test_blacklist_applied_on_first_drop(self):
        self.db.set_blacklist(["config"])
        try:
            view = self._view()
            view._on_parse_done(
                __import__("app.core.mrpack", fromlist=["MRPack"])
                .MRPack(name="t"), None, None, set())
            view.strategy_table.set_folders(["mods", "config"])
            # set_folders 之后再 set_blacklist 也必须生效
            view.strategy_table.set_blacklist(["config"])
            checked = view.strategy_table.get_checked()
            self.assertTrue(checked["mods"])
            self.assertFalse(checked["config"], "持久化黑名单第一次就不生效")
        finally:
            self.db.set_blacklist([])

    def test_parse_done_reads_blacklist_before_rows(self):
        """解析完成时黑名单必须先于建行设置（否则老版本会静默重新勾选）。"""
        src = (_ROOT / "app" / "ui" / "developer_view.py").read_text(
            encoding="utf-8")
        i_bl = src.index("self.strategy_table.set_blacklist(blacklist)")
        i_rows = src.index("self.strategy_table.set_folders(all_items")
        self.assertLess(i_bl, i_rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)

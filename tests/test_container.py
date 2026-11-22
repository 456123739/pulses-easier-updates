"""
test_container.py — 容器与存档变换的回归测试
------------------------------------------------
覆盖：

  C-1  v2 容器往返：多文件、跨块大文件、零字节文件、中文名
  C-2  跨块读取：单文件大于块大小时 chunk 引用正确
  C-3  存档变换：strip/restore 逐字节一致；损坏数据不被"修"坏
  C-4  变换失败自动退回原样存（不丢内容）
  C-5  旧包兼容：zip 仍然能读；签名能校验 zip 与 v2
  C-6  篡改检测：改一个字节签名就不过（v2 容器）
  C-7  损坏容器给出可读错误（不崩）
  C-8  路径安全：容器里的 `..`/绝对路径不会被解压出去
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core import container as ct
from app.core import mca
from app.core.packwriter import PackWriter


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="easier-ct-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ----------------------------------------------------------------------
class TestContainerRoundTrip(_Tmp):
    def _pack(self, files: dict[str, bytes], **kw) -> Path:
        out = self.tmp / "t.eapack"
        w = ct.ContainerWriter(out, **kw)
        for name, data in files.items():
            w.add_bytes(name, data)
        w.close()
        return out

    def test_roundtrip_small_files(self):
        files = {
            "modrinth.index.json": b'{"formatVersion":1}',
            "overrides/config/a.toml": b"k = 1\n" * 100,
            "overrides/mods/empty.jar": b"",
            "overrides/中文/配置.txt": "中文内容".encode("utf-8"),
        }
        out = self._pack(files)
        with ct.open_container(out) as r:
            self.assertEqual(set(r.names), set(files))
            for name, data in files.items():
                self.assertEqual(r.read(name), data, name)

    def test_large_file_spans_blocks(self):
        # 块大小压到 64 KB，喂 300 KB 随机数据 → 必然跨块
        data = os.urandom(300 * 1024)
        out = self._pack({"overrides/big.bin": data}, block_size=64 * 1024)
        with ct.open_container(out) as r:
            chunks = r.info("overrides/big.bin")["chunks"]
            self.assertGreater(len(chunks), 1, "大文件应该被切成多块")
            self.assertEqual(r.read("overrides/big.bin"), data)

    def test_streaming_matches_read(self):
        data = os.urandom(200 * 1024)
        out = self._pack({"a.bin": data}, block_size=64 * 1024)
        with ct.open_container(out) as r:
            self.assertEqual(b"".join(r.iter_chunks("a.bin")), data)

    def test_v2_is_smaller_than_zip_on_repetitive_data(self):
        body = (b"the quick brown fox " * 5000)
        files = {f"overrides/config/{i}.toml": body for i in range(6)}
        out = self._pack(files)
        z = self.tmp / "t.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        self.assertLess(out.stat().st_size, z.stat().st_size,
                        "跨文件重复内容时，块压缩应该比 zip 小")

    def test_reopen_after_close(self):
        out = self._pack({"a.txt": b"x" * 1000})
        for _ in range(3):
            with ct.open_container(out) as r:
                self.assertEqual(r.read("a.txt"), b"x" * 1000)


# ----------------------------------------------------------------------
class TestMcaTransform(_Tmp):
    def _region(self, payloads: list[bytes]) -> bytes:
        """造一个最小 region：每个区块 zlib 压缩，扇区对齐。"""
        header = bytearray(8192)
        body = bytearray()
        sector = 2
        for i, raw in enumerate(payloads):
            import struct
            import zlib
            payload = zlib.compress(raw, 6)
            rec = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
            rec += b"\x00" * ((-len(rec)) % 4096)
            sectors = len(rec) // 4096
            struct.pack_into(">I", header, i * 4, (sector << 8) | sectors)
            body += rec
            sector += sectors
        return bytes(header) + bytes(body)

    def test_roundtrip_is_byte_exact(self):
        data = self._region([os.urandom(3000) * 2, b"\x0a\x00\x01" * 900])
        blob = mca.strip_region(data)
        self.assertIsNotNone(blob)
        self.assertEqual(mca.restore_region(blob), data)

    def test_gaps_and_tails_survive(self):
        data = bytearray(self._region([b"\x0a\x00" * 500]))
        # 在空洞里塞"陈旧字节"（MC 不会清理这些位置）
        data[7000:7004] = b"\xde\xad\xbe\xef"
        data += b"tail-bytes"
        blob = mca.strip_region(bytes(data))
        self.assertEqual(mca.restore_region(blob), bytes(data))

    def test_broken_region_returns_none(self):
        self.assertIsNone(mca.strip_region(b"\x00" * 100))
        self.assertIsNone(mca.strip_region(b"\x00" * 8192))
        # 偏移指向垃圾 → 解压失败 → None（绝不打坏）
        bad = bytearray(8192 + 4096)
        import struct
        struct.pack_into(">I", bad, 0, (2 << 8) | 1)
        struct.pack_into(">I", bad, 8192, 999)          # 长度不合理
        self.assertIsNone(mca.strip_region(bytes(bad)))

    def test_transform_file_falls_back_when_level_differs(self):
        # 用 level 9 写一个 region：strip 后按 level 6 复原会不一致，
        # transform_file 必须自校验失败 → None
        import struct
        import zlib
        header = bytearray(8192)
        raw = b"\x0a\x00" * 800
        payload = zlib.compress(raw, 9)
        rec = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
        rec += b"\x00" * ((-len(rec)) % 4096)
        struct.pack_into(">I", header, 0, (2 << 8) | (len(rec) // 4096))
        p = self.tmp / "r.0.0.mca"
        p.write_bytes(bytes(header) + rec)
        self.assertIsNone(mca.transform_file(p),
                          "重新压缩档位不同就该放弃变换")

    def test_is_region_name(self):
        self.assertTrue(mca.is_region_name("saves/w/region/r.0.0.mca"))
        self.assertTrue(mca.is_region_name("saves/w/poi/r.0.0.mca"))
        self.assertFalse(mca.is_region_name("saves/w/entities/r.0.0.mca"))
        self.assertFalse(mca.is_region_name("mods/a.jar"))


# ----------------------------------------------------------------------
class TestPackWriter(_Tmp):
    def test_world_file_is_transformed_and_restored(self):
        import struct
        import zlib
        header = bytearray(8192)
        raw = b"\x0a\x00\x03" * 4000
        payload = zlib.compress(raw, 6)
        rec = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
        rec += b"\x00" * ((-len(rec)) % 4096)
        struct.pack_into(">I", header, 0, (2 << 8) | (len(rec) // 4096))
        src = self.tmp / "r.0.0.mca"
        src.write_bytes(bytes(header) + rec)

        out = self.tmp / "w.eapack"
        w = PackWriter(out, use_v2=True)
        w.add_bytes("modrinth.index.json", b"{}")
        w.add_file("overrides/saves/w/region/r.0.0.mca", src)
        w.finish({}, {"pack_name": "t"}, tamper_proof=False)

        with ct.open_container(out) as r:
            info = r.info("overrides/saves/w/region/r.0.0.mca")
            self.assertEqual(info["transform"], "mca", "应该走了存档变换")
            self.assertEqual(r.read("overrides/saves/w/region/r.0.0.mca"),
                             src.read_bytes(), "读出来必须是原始 .mca 字节")

    def test_signature_detects_tamper(self):
        out = self.tmp / "s.eapack"
        w = PackWriter(out, use_v2=True)
        w.add_bytes("modrinth.index.json", b'{"a":1}')
        w.add_bytes("changelog.md", b"# v1")
        w.add_bytes("ea_settings.json", b'{"x": 1}')   # 真导出一定会写
        w.finish({"x": 1}, {"pack_name": "t"}, tamper_proof=True)

        from app.core import eapack
        ok, reason = eapack.verify_signature(out)
        self.assertTrue(ok, reason)

        # 改内容（长度不变）但保持容器合法：改完重新写成 zip，
        # 这样走的才是"签名比对"而不是"块损坏"
        import packread
        bad = self.tmp / "bad.eapack"
        packread.rewrite_as_zip(
            out, bad, patch={"changelog.md": b"# v2"})
        ok2, reason2 = eapack.verify_signature(bad)
        self.assertFalse(ok2, "改内容后签名仍通过")
        self.assertIn("完整性", reason2)


# ----------------------------------------------------------------------
class TestCompatibility(_Tmp):
    def test_zip_still_readable(self):
        z = self.tmp / "old.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("modrinth.index.json", b"{}")
            zf.writestr("overrides/mods/a.jar", b"AAA")
        with ct.open_container(z) as r:
            self.assertIn("overrides/mods/a.jar", r.names)
            self.assertEqual(r.read("overrides/mods/a.jar"), b"AAA")
        from app.core import pack_detector
        self.assertEqual(pack_detector.detect_pack_kind(z)[0].value, "export")

    def test_corrupt_container_raises_readable_error(self):
        p = self.tmp / "broken.eapack"
        p.write_bytes(ct.MAGIC + b"\x00" * 40)
        with self.assertRaises(ct.ContainerError):
            ct.open_container(p).read("x")

    def test_unsafe_names_are_not_extracted(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        import packread
        out = self.tmp / "u.eapack"
        w = ct.ContainerWriter(out)
        w.add_bytes("../../evil.txt", b"x")
        w.add_bytes("mods/CON.jar", b"y")
        w.add_bytes("overrides/ok.txt", b"z")
        w.close()
        dest = self.tmp / "ex"
        packread.extract_all(out, dest)
        names = {p.relative_to(dest).as_posix()
                 for p in dest.rglob("*") if p.is_file()}
        self.assertEqual(names, {"overrides/ok.txt"})
        self.assertFalse((self.tmp / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()

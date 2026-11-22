"""JAR 去壳变换：逐字节可逆、失配 fallback、超级档开关。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core import container as ct


def make_jar(path: Path, level: int = 6, extra_stored: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nCreated-By: test\n")
        zf.writestr("assets/data.txt", "hello world\n" * 200,
                    compress_type=zipfile.ZIP_DEFLATED, compresslevel=level)
        zf.writestr("assets/code.bin", bytes(range(256)) * 64,
                    compress_type=zipfile.ZIP_DEFLATED, compresslevel=level)
        if extra_stored:
            zf.writestr("assets/raw.bin", b"raw" * 100,
                        compress_type=zipfile.ZIP_STORED)


class JarTransformCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-jar-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def _roundtrip(self, jar: Path, *, level=22, jar_transform=None, name="out.eapack"):
        out = self.tmp / name
        w = ct.ContainerWriter(out, level=level, jar_transform=jar_transform)
        w.add_file("overrides/mods/a.jar", jar)
        stats = w.close()
        original = jar.read_bytes()
        with ct.open_container(out) as r:
            got = r.read("overrides/mods/a.jar")
            ent = r.info("overrides/mods/a.jar")
        self.assertEqual(hashlib.sha256(got).digest(),
                         hashlib.sha256(original).digest())
        return ent, stats, out

    def test_super_level_auto_transforms_and_roundtrips(self):
        jar = self.tmp / "a.jar"; make_jar(jar)
        ent, stats, _ = self._roundtrip(jar)
        self.assertEqual(ent["transform"], "jar")
        self.assertEqual(stats.transformed, 1)
        self.assertGreater(ent["stored"], ent["size"])   # 存 raw，体积变大是正常的

    def test_explicit_jar_transform_at_normal_level(self):
        jar = self.tmp / "a.jar"; make_jar(jar)
        ent, _, _ = self._roundtrip(jar, level=17, jar_transform=True)
        self.assertEqual(ent["transform"], "jar")

    def test_normal_level_keeps_jar_raw(self):
        jar = self.tmp / "a.jar"; make_jar(jar)
        ent, stats, _ = self._roundtrip(jar, level=17)
        self.assertIsNone(ent["transform"])
        self.assertEqual(stats.transformed, 0)

    def test_fallback_when_recompress_not_byte_identical(self):
        jar = self.tmp / "a.jar"; make_jar(jar, level=9)
        if ct.jar_encode(jar) is not None:
            self.skipTest("当前 zlib 在 level9 也能复现，无法构造失配样例")
        ent, stats, _ = self._roundtrip(jar)
        self.assertIsNone(ent["transform"])
        self.assertEqual(stats.fallback_zip, False)

    def test_fallback_on_corrupt_entry(self):
        jar = self.tmp / "a.jar"; make_jar(jar)
        data = bytearray(jar.read_bytes())
        with zipfile.ZipFile(jar) as zf:
            zi = zf.getinfo("assets/data.txt")
        off = zi.header_offset
        nl, el = int.from_bytes(data[off + 26:off + 28], "little"), \
            int.from_bytes(data[off + 28:off + 30], "little")
        pos = off + 30 + nl + el + zi.compress_size - 1
        data[pos] ^= 0xFF
        jar.write_bytes(bytes(data))
        self.assertIsNone(ct.jar_encode(jar))
        out = self.tmp / "bad.eapack"
        w = ct.ContainerWriter(out, level=22)
        w.add_file("overrides/mods/a.jar", jar)
        w.close()
        with ct.open_container(out) as r:
            self.assertIsNone(r.info("overrides/mods/a.jar")["transform"])
            self.assertEqual(r.read("overrides/mods/a.jar"), bytes(data))

    def test_corrupt_jar_blob_rejected(self):
        jar = self.tmp / "a.jar"; make_jar(jar)
        blob = ct.jar_encode(jar)
        self.assertIsNotNone(blob)
        with self.assertRaises(ct.ContainerError):
            ct.jar_decode(b"JAR1" + b"\x00" * 4)


if __name__ == "__main__":
    unittest.main()

"""文件指纹：与独立实现交叉验证 + 缓存。"""

import hashlib
import random
import struct
import tempfile
import unittest
from pathlib import Path

from app.core import fingerprint as F


def ref_murmur2(data: bytes) -> int:
    """独立实现（struct 版），仅用于测试交叉验证。"""
    d = bytes(b for b in data if b not in (9, 10, 13, 32))
    h = (1 ^ len(d)) & 0xFFFFFFFF
    M = 0x5BD1E995
    i = 0
    while len(d) - i >= 4:
        k = struct.unpack_from("<I", d, i)[0]
        k = (k * M) & 0xFFFFFFFF
        k ^= k >> 24
        k = (k * M) & 0xFFFFFFFF
        h = (h * M) & 0xFFFFFFFF
        h ^= k
        i += 4
    rest = d[i:]
    if len(rest) == 3:
        h ^= rest[0] | rest[1] << 8
        h ^= rest[2] << 16
        h = (h * M) & 0xFFFFFFFF
    elif len(rest) == 2:
        h ^= rest[0] | rest[1] << 8
        h = (h * M) & 0xFFFFFFFF
    elif len(rest) == 1:
        h ^= rest[0]
        h = (h * M) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * M) & 0xFFFFFFFF
    h ^= h >> 15
    return h & 0xFFFFFFFF


class MurmurCase(unittest.TestCase):
    def test_empty_and_whitespace_equal(self):
        self.assertEqual(F.curseforge_fingerprint(b""),
                         F.curseforge_fingerprint(b"\t\n\r  "))

    def test_random_blobs_match_reference(self):
        rnd = random.Random(11)
        for n in list(range(0, 33)) + [rnd.randrange(1, 4000) for _ in range(60)]:
            data = bytes(rnd.randrange(256) for _ in range(n))
            self.assertEqual(F.curseforge_fingerprint(data), ref_murmur2(data),
                             f"len={n}")

    def test_whitespace_inside_filtered(self):
        a = b"abc def"
        b = b"abcdef"
        self.assertEqual(F.curseforge_fingerprint(a), F.curseforge_fingerprint(b))


class ComputeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-fp-"))

    def test_triple_hash_matches_stdlib(self):
        path = self.tmp / "a.bin"
        data = bytes(range(256)) * 40 + b"\t\n\r  "
        path.write_bytes(data)
        fp = F.compute(path)
        self.assertEqual(fp.sha1, hashlib.sha1(data).hexdigest())
        self.assertEqual(fp.sha512, hashlib.sha512(data).hexdigest())
        self.assertEqual(fp.curseforge, F.curseforge_fingerprint(data))
        self.assertEqual(fp.size, len(data))

    def test_large_file_second_pass_branch(self):
        path = self.tmp / "big.bin"
        chunk = bytes(range(256)) * 4096          # 1 MiB
        with open(path, "wb") as fh:
            for _ in range(34):                   # 34 MiB > _MEM_CAP
                fh.write(chunk)
        fp = F.compute(path)
        raw = path.read_bytes()
        self.assertEqual(fp.curseforge, F.curseforge_fingerprint(raw))


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-fpc-"))
        self.cache = F.FingerprintCache(self.tmp / "fp.json")

    def test_roundtrip_and_invalidation(self):
        path = self.tmp / "x.jar"
        path.write_bytes(b"hello")
        first = self.cache.compute(path)
        self.assertEqual(self.cache.compute(path), first)
        self.assertEqual(self.cache.get(path), first)

        path.write_bytes(b"hello!")
        self.assertIsNone(self.cache.get(path))
        second = self.cache.compute(path)
        self.assertNotEqual(first.sha1, second.sha1)

    def test_cache_file_written(self):
        path = self.tmp / "y.jar"
        path.write_bytes(b"data")
        self.cache.compute(path)
        self.cache.flush()                 # 批量落盘：显式 flush 才写文件
        self.assertTrue((self.tmp / "fp.json").is_file())


if __name__ == "__main__":
    unittest.main()

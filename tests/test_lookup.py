"""联网查询客户端：批量、命中判定、失败降级（全部用假 fetch）。"""

import threading
import unittest
import urllib.error

from app.core import lookup as L


def cf_hit(fp: int, url="https://edge.forgecdn.net/x.jar", size=10, name="x.jar"):
    return {"data": {"exactMatches": [{
        "id": 123, "slug": "demo",
        "file": {"fileFingerprint": fp, "downloadUrl": url,
                 "fileName": name, "fileLength": size,
                 "displayName": "Demo 1.0"},
    }]}}


class FakeFetch:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers))
        return self.responder(url, payload)


class CurseForgeCase(unittest.TestCase):
    def test_match_and_headers(self):
        fake = FakeFetch(lambda url, p, h=None, t=None: cf_hit(p["fingerprints"][0]))
        client = L.CurseForgeClient("KEY", fetch=fake)
        hits = client.match([111])
        self.assertIn(111, hits)
        self.assertEqual(hits[111].source, "curseforge")
        self.assertEqual(hits[111].urls, ["https://edge.forgecdn.net/x.jar"])
        url, payload, headers = fake.calls[0]
        self.assertTrue(url.endswith("/fingerprints/432"))
        self.assertEqual(headers["x-api-key"], "KEY")

    def test_no_download_url_is_miss(self):
        fake = FakeFetch(lambda url, p, h=None, t=None: cf_hit(p["fingerprints"][0], url=""))
        self.assertEqual(L.CurseForgeClient("K", fetch=fake).match([1]), {})

    def test_unknown_fingerprint_ignored(self):
        fake = FakeFetch(lambda url, p, h=None, t=None: cf_hit(999))
        self.assertEqual(L.CurseForgeClient("K", fetch=fake).match([1]), {})

    def test_batching(self):
        fake = FakeFetch(lambda url, p, h=None, t=None: {"data": {"exactMatches": []}})
        L.CurseForgeClient("K", fetch=fake).match(list(range(1201)))
        self.assertEqual(len(fake.calls), 3)

    def test_403_raises_denied(self):
        def boom(url, p, headers=None, timeout=None):
            raise urllib.error.HTTPError(url, 403, "forbidden", None, None)
        with self.assertRaises(L.LookupDenied):
            L.CurseForgeClient("bad", fetch=boom).match([1])

    def test_retry_then_success(self):
        state = {"n": 0}

        def flaky(url, p, headers=None, timeout=None):
            state["n"] += 1
            if state["n"] < 3:
                raise urllib.error.HTTPError(url, 429, "slow down", None, None)
            return cf_hit(p["fingerprints"][0])

        hits = L.CurseForgeClient("K", fetch=flaky, sleep=lambda s: None).match([7])
        self.assertIn(7, hits)
        self.assertEqual(state["n"], 3)

    def test_empty_key(self):
        with self.assertRaises(L.LookupDenied):
            L.CurseForgeClient("").match([1])


def mr_version(sha1: str, url="https://cdn.modrinth.com/x.jar", extra_file=None):
    files = [{"hashes": {"sha1": "other" * 8}, "url": "https://bad", "size": 1,
              "filename": "bad.jar"},
             {"hashes": {"sha1": sha1}, "url": url, "size": 20,
              "filename": "good.jar", "primary": True}]
    if extra_file:
        files = extra_file
    return {sha1: {"project_id": "P1", "version_number": "1.2.3", "files": files}}


class ModrinthCase(unittest.TestCase):
    def test_picks_file_with_matching_sha1(self):
        sha = "a" * 40
        fake = FakeFetch(lambda url, p, h=None, t=None: mr_version(sha))
        hits = L.ModrinthClient("UA", fetch=fake).match([sha])
        self.assertEqual(hits[sha].urls, ["https://cdn.modrinth.com/x.jar"])
        self.assertEqual(hits[sha].size, 20)

    def test_key_present_but_no_matching_file(self):
        sha = "b" * 40
        fake = FakeFetch(lambda url, p, h=None, t=None: mr_version("c" * 40))
        self.assertEqual(L.ModrinthClient("UA", fetch=fake).match([sha]), {})

    def test_user_agent_sent(self):
        fake = FakeFetch(lambda url, p, h=None, t=None: {})
        L.ModrinthClient("pulses/1.0", fetch=fake).match(["d" * 40])
        self.assertEqual(fake.calls[0][2]["User-Agent"], "pulses/1.0")


class ResolveCase(unittest.TestCase):
    def test_cf_first_then_mr(self):
        calls = []

        def responder(url, payload, headers=None, timeout=None):
            calls.append(url)
            if "curseforge" in url:
                return {"data": {"exactMatches": [
                    {"id": f, "slug": f"m{f}",
                     "file": {"fileFingerprint": f,
                              "downloadUrl": f"https://edge.forgecdn.net/{f}.jar",
                              "fileName": f"{f}.jar", "fileLength": 1,
                              "displayName": f"{f}"}}
                    for f in payload["fingerprints"]]}}
            return mr_version(payload["hashes"][0])

        cands = [{"path": "mods/a.jar", "sha1": "a" * 40, "curseforge": 11},
                 {"path": "mods/b.jar", "sha1": "b" * 40, "curseforge": 22}]
        hits = L.resolve(cands, cf_key="K", user_agent="UA",
                         fetch=responder, sleep=lambda s: None)
        self.assertEqual(hits["mods/a.jar"].source, "curseforge")
        self.assertEqual(hits["mods/b.jar"].source, "curseforge")
        self.assertEqual(sum(1 for c in calls if "modrinth" in c), 0)

    def test_mr_covers_cf_misses(self):
        def responder(url, payload, headers=None, timeout=None):
            if "curseforge" in url:
                return {"data": {"exactMatches": []}}
            return mr_version(payload["hashes"][0])

        cands = [{"path": "mods/a.jar", "sha1": "a" * 40, "curseforge": 11}]
        hits = L.resolve(cands, cf_key="K", user_agent="UA",
                         fetch=responder, sleep=lambda s: None)
        self.assertEqual(hits["mods/a.jar"].source, "modrinth")

    def test_cf_down_still_uses_mr(self):
        def responder(url, payload, headers=None, timeout=None):
            if "curseforge" in url:
                raise urllib.error.HTTPError(url, 500, "boom", None, None)
            return mr_version(payload["hashes"][0])

        cands = [{"path": "x.jar", "sha1": "e" * 40, "curseforge": 5}]
        hits = L.resolve(cands, cf_key="K", user_agent="UA",
                         fetch=responder, sleep=lambda s: None)
        self.assertEqual(hits["x.jar"].source, "modrinth")

    def test_identical_content_shares_hit(self):
        def responder(url, payload, headers=None, timeout=None):
            if "curseforge" in url:
                return {"data": {"exactMatches": [
                    {"id": 1, "slug": "x", "file": {
                        "fileFingerprint": payload["fingerprints"][0],
                        "downloadUrl": "https://edge.forgecdn.net/x.jar",
                        "fileName": "x.jar", "fileLength": 1,
                        "displayName": "x"}}]}}
            return {}

        cands = [{"path": "mods/a.jar", "sha1": "a" * 40, "curseforge": 7},
                 {"path": "mods/copy-of-a.jar", "sha1": "a" * 40,
                  "curseforge": 7}]
        hits = L.resolve(cands, cf_key="K", user_agent="UA",
                         fetch=responder, sleep=lambda s: None)
        self.assertEqual(hits["mods/a.jar"].urls, hits["mods/copy-of-a.jar"].urls)

    def test_both_down_returns_empty(self):
        def responder(url, payload, headers=None, timeout=None):
            raise OSError("no network")

        cands = [{"path": "x.jar", "sha1": "f" * 40, "curseforge": 5}]
        hits = L.resolve(cands, cf_key="K", user_agent="UA",
                         fetch=responder, sleep=lambda s: None)
        self.assertEqual(hits, {})

    def test_cancel_stops_early(self):
        ev = threading.Event()
        ev.set()
        cands = [{"path": "x.jar", "sha1": "a" * 40, "curseforge": 1}]
        with self.assertRaises(L.LookupError):
            L.CurseForgeClient("K", fetch=lambda *a: cf_hit(1)).match(
                [1], cancel=ev)


if __name__ == "__main__":
    unittest.main()

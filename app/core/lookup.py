"""
lookup.py — 把本地文件 fingerprint 换成「下载地址」
------------------------------------------------
两条路（PCL 同款接口，2026-11-01 用真实文件验证过，见
docs/PCL-整合包导出-技术规范.md §3 / §10）：

  CurseForge  POST /v1/fingerprints/432      {"fingerprints":[murmur2…]}
  Modrinth    POST /v2/version_files         {"hashes":[sha1…],"algorithm":"sha1"}

策略：**CF 先查**（一般更快），没命中的再查 MR；两边都没有的留在本地打包。

这里只做「查询 → 规范化结果」，不做下载、不碰磁盘（除了缓存由上层负责）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

CF_BASE = "https://api.curseforge.com/v1"
MR_BASE = "https://api.modrinth.com/v2"
BATCH = 500


class LookupError(RuntimeError):
    """查询失败（网络/授权）。调用方应降级为「本地打包」。"""


class LookupDenied(LookupError):
    """API Key 无效或权限不足（403）。"""


@dataclass
class LookupHit:
    source: str                       # "curseforge" | "modrinth"
    urls: list[str] = field(default_factory=list)
    file_name: str = ""
    size: int = 0
    project: str = ""
    version: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "urls": list(self.urls),
                "fileName": self.file_name, "size": self.size,
                "project": self.project, "version": self.version}


def _default_fetch(url: str, payload: dict | None, headers: dict,
                   timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _call(fetch, url: str, payload: dict, headers: dict, *,
          timeout: float, retries: int, sleep: Callable[[float], None],
          cancel: threading.Event | None = None) -> dict:
    """带重试的请求：429/5xx 退避，403 直接判授权失败。"""
    delay = 1.0
    last: Exception | None = None
    for attempt in range(retries + 1):
        if cancel is not None and cancel.is_set():
            raise LookupError("已取消")
        try:
            return fetch(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise LookupDenied(f"API 授权失败（403）：{url}") from exc
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise LookupError(f"HTTP {exc.code}：{url}") from exc
        except Exception as exc:                    # noqa: BLE001
            last = exc
        if attempt < retries:
            sleep(delay)
            delay = min(delay * 2, 8.0)
    raise LookupError(f"请求失败：{url}（{last}）")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class CurseForgeClient:
    def __init__(self, api_key: str, *, fetch=_default_fetch, timeout: float = 30,
                 retries: int = 2, sleep=time.sleep) -> None:
        self.api_key = (api_key or "").strip()
        self.fetch = fetch
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def match(self, fingerprints: list[int], *,
              cancel: threading.Event | None = None) -> dict[int, LookupHit]:
        out: dict[int, LookupHit] = {}
        if not self.api_key:
            raise LookupDenied("没有配置 CurseForge API Key")
        headers = {"x-api-key": self.api_key,
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        for part in _chunks([int(f) for f in fingerprints], BATCH):
            data = _call(self.fetch, f"{CF_BASE}/fingerprints/432",
                         {"fingerprints": part}, headers,
                         timeout=self.timeout, retries=self.retries,
                         sleep=self.sleep, cancel=cancel)
            payload = (data or {}).get("data") or {}
            for item in payload.get("exactMatches") or []:
                file = (item or {}).get("file") or {}
                url = str(file.get("downloadUrl") or "")
                fp = int(file.get("fileFingerprint") or 0)
                if not url or fp not in part:
                    continue        # 没有直链等于没命中（PCL 同款判断）
                out[fp] = LookupHit(
                    source="curseforge", urls=[url],
                    file_name=str(file.get("fileName") or ""),
                    size=int(file.get("fileLength") or 0),
                    project=str((item or {}).get("slug")
                                or (item or {}).get("id") or ""),
                    version=str(file.get("displayName") or ""))
        return out


class ModrinthClient:
    def __init__(self, user_agent: str, *, fetch=_default_fetch,
                 timeout: float = 30, retries: int = 2,
                 sleep=time.sleep) -> None:
        self.ua = user_agent
        self.fetch = fetch
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def match(self, sha1s: list[str], *,
              cancel: threading.Event | None = None) -> dict[str, LookupHit]:
        out: dict[str, LookupHit] = {}
        headers = {"User-Agent": self.ua, "Content-Type": "application/json",
                   "Accept": "application/json"}
        for part in _chunks([str(s).lower() for s in sha1s], BATCH):
            data = _call(self.fetch, f"{MR_BASE}/version_files",
                         {"hashes": part, "algorithm": "sha1"}, headers,
                         timeout=self.timeout, retries=self.retries,
                         sleep=self.sleep, cancel=cancel)
            for sha1, version in (data or {}).items():
                if sha1 not in part:
                    continue
                # 只有 files[] 里 sha1 真的一致才算命中（不能只看 key）
                hit_file = None
                for file in (version or {}).get("files") or []:
                    hashes = (file or {}).get("hashes") or {}
                    if str(hashes.get("sha1") or "").lower() == sha1:
                        hit_file = file
                        break
                if hit_file is None:
                    continue
                out[sha1] = LookupHit(
                    source="modrinth",
                    urls=[str(hit_file.get("url") or "")],
                    file_name=str(hit_file.get("filename") or ""),
                    size=int(hit_file.get("size") or 0),
                    project=str((version or {}).get("project_id") or ""),
                    version=str((version or {}).get("version_number") or ""))
        return out


# ----------------------------------------------------------------------
# 编排：CF 先，MR 兜底
# ----------------------------------------------------------------------
def resolve(candidates: list[dict], *, cf_key: str, user_agent: str,
            fetch=_default_fetch, cancel: threading.Event | None = None,
            progress: Callable[[str, int, int], None] | None = None,
            sleep=time.sleep) -> dict[str, LookupHit]:
    """candidates: [{path, sha1, curseforge}] → {path: LookupHit}。

    CF / MR 任一失败都不抛出（降级为"这些文件本地打包"），
    只有两者都不可用时才由上层决定是否提示。
    """
    result: dict[str, LookupHit] = {}
    by_cf: dict[int, list[dict]] = {}
    for cand in candidates:
        if cand.get("curseforge"):
            by_cf.setdefault(int(cand["curseforge"]), []).append(cand)
    by_sha1: dict[str, list[dict]] = {}
    for cand in candidates:
        if cand.get("sha1"):
            by_sha1.setdefault(str(cand["sha1"]).lower(), []).append(cand)

    done = 0
    if by_cf:
        try:
            hits = CurseForgeClient(cf_key, fetch=fetch, sleep=sleep).match(
                list(by_cf), cancel=cancel)
        except LookupError:
            hits = {}
        for fp, hit in hits.items():
            for cand in by_cf.get(fp, ()):      # 同内容文件 → 共用同一个直链
                result[cand["path"]] = hit
        done = len(by_cf)
        if progress:
            progress("curseforge", done, len(by_cf))

    rest = [c for c in candidates if c["path"] not in result and c.get("sha1")]
    if rest and not (cancel and cancel.is_set()):
        try:
            hits = ModrinthClient(user_agent, fetch=fetch, sleep=sleep).match(
                [c["sha1"] for c in rest], cancel=cancel)
        except LookupError:
            hits = {}
        for sha1, hit in hits.items():
            for cand in by_sha1.get(sha1, ()):
                if cand["path"] not in result:
                    result[cand["path"]] = hit
        if progress:
            progress("modrinth", len(rest), len(rest))
    return result

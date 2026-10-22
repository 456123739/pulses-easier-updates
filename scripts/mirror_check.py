#!/usr/bin/env python3
"""
mirror_check.py — 镜像站自检
=============================
对 `app/core/mirrors.py` 里登记的每个镜像实测一次：能不能下、首字节多快、
每秒多少字节，然后按速度排序打印。官方 CDN 也测一遍作为基线。

用法：

    python scripts/mirror_check.py                # 测全部（含已停用的，标出来）
    python scripts/mirror_check.py --only-enabled # 只测启用中的
    python scripts/mirror_check.py --bytes 4194304
    python scripts/mirror_check.py --url <官方文件地址>
    python scripts/mirror_check.py --json

结果怎么用：
  * 明显比官方快的 → 把对应条目在 mirrors.py 里 `enabled=True`（或填到设置的
    「镜像地址」里），下次下载就会优先走它；
  * 全都不行 → 保持现状，程序会自动回退官方 CDN，不会影响下载。
"""

import argparse
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import mirrors  # noqa: E402

# 默认拿一个小文件测（Sodium 的 fabric 包，约 1 MB）
DEFAULT_URL = ("https://cdn.modrinth.com/data/AANobbMI/versions/g6ju16VJ/"
               "sodium-fabric-0.9.3-beta.1%2Bmc26.3.jar")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_SSL = ssl.create_default_context()


def measure(url: str, want: int, timeout: float) -> dict:
    """测一次：返回 {ok, http, bytes, ttfb_ms, speed, error}。"""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Encoding": "identity",
            "Range": f"bytes=0-{want - 1}",
        })
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_SSL) as resp:
            ttfb = (time.monotonic() - t0) * 1000
            got = 0
            while got < want:
                block = resp.read(min(65536, want - got))
                if not block:
                    break
                got += len(block)
            cost = max(time.monotonic() - t0, 1e-6)
            return {"ok": got > 0, "http": getattr(resp, "status", 200),
                    "bytes": got, "ttfb_ms": round(ttfb, 1),
                    "speed": round(got / cost / 1024, 1), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "http": 0, "bytes": 0, "ttfb_ms": 0,
                "speed": 0.0, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="镜像站自检")
    ap.add_argument("--url", default=DEFAULT_URL, help="用来测的官方文件地址")
    ap.add_argument("--bytes", type=int, default=1024 * 1024,
                    help="每个镜像拉多少字节（默认 1 MiB）")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--only-enabled", action="store_true",
                    help="只测启用中的镜像")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    host, path = urllib.parse.urlsplit(args.url).netloc.lower(), \
        urllib.parse.urlsplit(args.url).path
    kind = mirrors.HOST_KIND.get(host, "modrinth")

    rows = [{
        "key": "official", "name": "官方 CDN", "kind": kind,
        "enabled": True, "url": args.url,
        "note": "基线", "checked": "",
        **measure(args.url, args.bytes, args.timeout),
    }]

    for site in mirrors.sites(include_disabled=not args.only_enabled):
        if kind not in site.kinds:
            continue
        url = site.build(args.url, path)
        rows.append({
            "key": site.key, "name": site.name, "kind": kind,
            "enabled": site.enabled, "url": url,
            "note": site.note, "checked": site.checked,
            **measure(url, args.bytes, args.timeout),
        })

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    rows.sort(key=lambda r: (not r["ok"], -r["speed"]))
    base = next((r["speed"] for r in rows if r["key"] == "official"), 0.0)
    print(f"\n测试文件：{args.url}")
    print(f"每项拉取：{args.bytes / 1024:.0f} KiB\n")
    head = f"{'状态':<6}{'镜像':<26}{'HTTP':<6}{'首字节':>9}{'速度':>12}{'相对官方':>10}"
    print(head)
    print("-" * len(head))
    for r in rows:
        state = "✓" if r["ok"] else "✗"
        if r["key"] != "official" and not r["enabled"]:
            state = "(停用)"
        ttfb = f"{r['ttfb_ms']:.0f}ms" if r["ok"] else "-"
        spd = f"{r['speed']:.1f} KB/s" if r["ok"] else "-"
        ratio = ""
        if r["ok"] and base > 0:
            ratio = f"{r['speed'] / base:.1f}×"
        print(f"{state:<6}{r['name'][:24]:<26}{r['http']:<6}{ttfb:>9}"
              f"{spd:>12}{ratio:>10}")
        if r["error"]:
            print(f"       └ {r['error'][:90]}")
    print("\n提示：明显快于官方的条目，把 mirrors.py 里对应 MirrorSite 的 "
          "enabled 改成 True 即可；全都不行就保持现状（自动回退官方）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

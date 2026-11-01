"""给 e2e_export.mjs 造一个整合包实例。

默认带一个**真实** Modrinth 模组（联网可取时），这样"查直链 → 写 index"
这条路径在 CI 里也能被覆盖；取不到就退回假 jar，脚本仍能跑（index 允许为 0）。
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "pulses-easier/ci (fixture)"}
# 体积小、长期存在：sodium（fabric）
PROJECT = "sodium"


def fetch_real_mod(dest: Path) -> bool:
    try:
        query = urllib.parse.urlencode(
            {"game_versions": json.dumps(["1.20.1"]),
             "loaders": json.dumps(["fabric"])},
            quote_via=urllib.parse.quote)
        url = f"https://api.modrinth.com/v2/project/{PROJECT}/version?{query}"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            versions = json.loads(resp.read())
        file = versions[0]["files"][0]
        req = urllib.request.Request(file["url"], headers=UA)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if len(data) < 10_000:
            return False
        dest.write_bytes(data)
        print(f"真实模组：{file['filename']}（{len(data):,} B）")
        return True
    except Exception as exc:                            # noqa: BLE001
        print(f"取不到真实模组（{exc}），退回假 jar")
        return False


def main(raw: str) -> int:
    root = Path(raw).expanduser()
    if root.exists():
        shutil.rmtree(root)
    pack = root / "DemoPack"
    (pack / "mods").mkdir(parents=True)
    (pack / "config").mkdir()
    if not fetch_real_mod(pack / "mods" / "sodium.jar"):
        (pack / "mods" / "fake.jar").write_bytes(b"FAKE" * 4096)
    (pack / "mods" / "local-only.jar").write_bytes(b"LOCAL" * 512)
    (pack / "config" / "demo.toml").write_text("k = 1\n", encoding="utf-8")
    (pack / "options.txt").write_text("fov:90\n", encoding="utf-8")
    (pack / f"{pack.name}.json").write_text(json.dumps({
        "id": "1.20.1-fabric-0.15.11",
        "inheritsFrom": "fabric-loader-0.15.11-1.20.1",
    }), encoding="utf-8")
    print(pack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/export-fixture"))

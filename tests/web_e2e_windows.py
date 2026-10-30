"""真机端到端（Windows CI）：启动新界面 → 在真页面里点完整流程 → 查磁盘。

怎么做到"无人值守"：`app/ui_web/e2e_driver.py` 在真实 WebView2 页面里用
`evaluate_js` 点按钮、读文本（和玩家操作同一条路径），结果写 JSON。
这个脚本只负责：搭夹具 → 起进程 → 等报告 → 核对磁盘。

非 Windows 直接跳过（返回 0），本地不装任何额外依赖也不会红。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = float(os.environ.get("PULSES_E2E_TIMEOUT", "300"))


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def build_fixture(tmp: Path) -> tuple[Path, Path]:
    from app.core import database as db
    from app.core import eapack as eapack_mod
    from app.core.mrpack import merge_files, parse_mrpack

    dbdir = tmp / "db"
    db.create_database(dbdir)
    db.set_db_path(dbdir)

    pack = tmp / "versions" / "E2EPack"
    (pack / "mods").mkdir(parents=True)
    (pack / "config").mkdir(parents=True)
    (pack / "mods" / "old.jar").write_text("OLD", encoding="utf-8")
    (pack / "mods" / "gone.jar").write_text("LOCAL", encoding="utf-8")
    (pack / "config" / "mine.toml").write_text("mine = 1\n", encoding="utf-8")
    (pack / "config" / "sodium-options.json").write_text(
        '{"render_distance": 8}\n', encoding="utf-8")

    src = tmp / "src"
    (src / "overrides" / "mods").mkdir(parents=True)
    (src / "overrides" / "config").mkdir(parents=True)
    (src / "modrinth.index.json").write_text(json.dumps({
        "formatVersion": 1, "game": "minecraft", "versionId": "3.0.0",
        "name": "E2EPack", "files": [],
        "dependencies": {"minecraft": "1.20.1"},
    }), encoding="utf-8")
    (src / "overrides" / "mods" / "old.jar").write_text("NEW", encoding="utf-8")
    (src / "overrides" / "mods" / "added.jar").write_text("ADDED", encoding="utf-8")
    (src / "overrides" / "config" / "sodium-options.json").write_text(
        '{"render_distance": 16}\n', encoding="utf-8")

    src_zip = tmp / "E2EPack.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())

    parsed, err = parse_mrpack(src_zip)
    assert parsed is not None, err
    update = tmp / "E2EPack-3.0.0.eapack"
    out = eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=update,
        pack=parsed, merged_files=merge_files(parsed),
        strategies={"mods": "完全匹配"}, changelog_md="# 3.0.0\n\n- e2e\n")
    assert out is not None
    return pack, update


def main() -> int:
    if sys.platform != "win32":
        log("非 Windows，跳过真机 e2e")
        return 0

    # 产物必须已构建：CI 里这一步在 npm run build 之后
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        log("缺少 web/dist，先构建前端")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pe-e2e-"))
    proc = None
    try:
        pack, update = build_fixture(tmp)
        home = tmp / "home"
        home.mkdir()
        report = tmp / "report.json"
        env = {
            **os.environ,
            "PULSES_UI": "web",
            "PULSES_E2E": "1",
            "PULSES_E2E_PACK": str(pack),
            "PULSES_E2E_UPDATE": str(update),
            "PULSES_E2E_REPORT": str(report),
            "HOME": str(home),
            "USERPROFILE": str(home),
        }
        exe = Path(sys.executable)
        cmd = [str(exe)] if getattr(sys, "frozen", False) else [
            str(exe), str(ROOT / "main.py")]
        log("启动：" + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")

        end = time.time() + TIMEOUT
        while time.time() < end:
            if report.is_file():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)

        out = ""
        if proc.poll() is None:
            proc.terminate()
        try:
            out = proc.communicate(timeout=20)[0] or ""
        except Exception:                               # noqa: BLE001
            proc.kill()
        if out.strip():
            log("程序输出：\n" + out[-4000:])

        if not report.is_file():
            log("没有拿到 e2e 报告（程序没跑到驱动阶段）")
            return 1
        data = json.loads(report.read_text(encoding="utf-8"))
        for s in data.get("steps", []):
            log(f"{'PASS' if s['ok'] else 'FAIL'}  {s['name']}"
                + (f"  → {s['detail']}" if not s["ok"] else ""))
        if not data.get("ok"):
            log("失败：" + str(data.get("error")))
            return 1

        # 磁盘为准（界面说完成不算）
        assert (pack / "mods" / "old.jar").read_text() == "NEW"
        assert (pack / "mods" / "added.jar").read_text() == "ADDED"
        assert not (pack / "mods" / "gone.jar").exists()
        assert (pack / "config" / "mine.toml").read_text() == "mine = 1\n"
        assert (pack / "config" / "sodium-options.json").read_text() == \
            '{"render_distance": 8}\n'
        log("磁盘结果正确（含智能剔除保护）")
        return 0
    except Exception:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

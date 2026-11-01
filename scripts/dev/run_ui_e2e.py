"""
run_ui_e2e.py — 一条命令跑完"真前端 + 真 Api + 真流程"的端到端
------------------------------------------------
1. 基本流程：造夹具（整合包 + 更新包）→ 起 bridge_server（真 Api）
   → Playwright 在真页面里点完所有功能 → 核对磁盘 → 校验开发者导出的 .eapack
2. 真下载：造"带 URL 文件"的夹具 → 起 file_server（支持 Range、限速）
   → 再起一个 bridge_server → 下载/进度/中止/续传 → 核对磁盘字节

两套都跑在临时目录里，HOME/USERPROFILE 也指向临时目录：
玩家真实的 ~/.pulses_easier 不会被读，更不会被写。

CI 里直接 `python scripts/dev/run_ui_e2e.py`；本地同样能跑（需要
`npm ci && npx playwright install chromium`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:                # 直接 python scripts/dev/xxx.py
    sys.path.insert(0, str(ROOT))
WEB = ROOT / "web"
DEV = ROOT / "scripts" / "dev"
E2E_JS = DEV / "e2e_ui.mjs"
TIMEOUT_NODE = float(os.environ.get("PULSES_E2E_TIMEOUT", "600"))


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def check(ok: bool, label: str, extra: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}"
          + ("" if ok or not extra else f"  →  {extra}"), flush=True)
    return bool(ok)


def wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def child_env(home: Path) -> dict:
    """子进程一律跑在临时 HOME 里（Windows 认 USERPROFILE）。"""
    env = {**os.environ,
           "HOME": str(home),
           "USERPROFILE": str(home),
           "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8"}
    env.pop("PULSES_UI", None)
    env.pop("PULSES_E2E", None)
    return env


def run_fixture(script: Path, out_dir: Path, env: dict, *args: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), str(out_dir), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"夹具失败：{script.name}\n"
                           f"{(proc.stderr or proc.stdout)[-3000:]}")
    path = out_dir / "fixture.json"
    if not path.is_file():
        raise RuntimeError(f"夹具没有写 fixture.json：{out_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def start(cmd: list[str], env: dict, name: str) -> subprocess.Popen:
    log(f"启动 {name}：{' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def run_node(port: int, work: Path, suite: str) -> int:
    url = f"http://127.0.0.1:{port}/index.html"
    log(f"Playwright：{suite}")
    try:
        proc = subprocess.run(
            ["node", str(E2E_JS), url, str(work), suite],
            cwd=str(ROOT), timeout=TIMEOUT_NODE)
    except FileNotFoundError:
        log("找不到 node：请先装 Node 22 并 npm ci")
        return 127
    except subprocess.TimeoutExpired:
        log(f"{suite} 超时（>{TIMEOUT_NODE:.0f}s）")
        return 124
    return proc.returncode


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_exported(path: Path) -> bool:
    """开发者端导出的 .eapack：只有 Python 才读得懂，必须在这里复核。"""
    from app.core import eapack as eapack_mod

    ok = True
    ok &= check(path.is_file(), "导出包存在", str(path))
    if not path.is_file():
        return False
    ok &= check(path.stat().st_size > 500, "导出包不是空壳",
                f"{path.stat().st_size} B")
    settings = eapack_mod.read_settings(path) or {}
    manifest = eapack_mod.read_manifest(path) or {}
    hashes = eapack_mod.read_hashes(path) or {}
    changelog = eapack_mod.read_changelog(path) or ""
    strategies = settings.get("recommended_strategies") or {}
    ok &= check(strategies.get("mods") == "替换重名",
                "导出包带上了界面改过的策略（mods=替换重名）",
                json.dumps(strategies, ensure_ascii=False))
    ok &= check("config" in (manifest.get("excluded_folders") or []),
                "导出包记录了被排除的目录（config）",
                json.dumps(manifest.get("excluded_folders")))
    ok &= check(manifest.get("pack_name") == "E2EPack"
                and manifest.get("pack_version") == "3.0.0",
                "导出包清单里的包名/版本正确",
                json.dumps({k: manifest.get(k) for k in
                            ("pack_name", "pack_version")}, ensure_ascii=False))
    ok &= check("开发者端" in changelog, "导出包里的更新日志是界面填的",
                changelog[:60])
    ok &= check("mods/old.jar" in (hashes.get("files") or {}),
                "导出包写了文件哈希清单")
    signed, reason = eapack_mod.verify_signature(path)
    ok &= check(bool(signed) and not reason, "导出包签名校验通过", reason or "")
    with zipfile.ZipFile(path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    ok &= check(not any(n.startswith("overrides/config/") for n in names),
                "被排除的目录没有进包",
                json.dumps([n for n in names if "config" in n][:5]))
    ok &= check("overrides/mods/old.jar" in names and "ea_settings.json" in names,
                "导出包内容齐全")
    return ok


def verify_downloaded(fixture: dict) -> bool:
    ok = True
    pack = Path(fixture["pack"])
    for item in fixture.get("files") or []:
        target = pack / item["path"]
        ok &= check(target.is_file() and sha256(target) == item["sha256"],
                    f"下载字节与源一致：{item['name']}（{item['size']} B）",
                    str(target))
    abort_target = Path(fixture["abortPack"]) / fixture["abortFile"]["path"]
    ok &= check(abort_target.is_file()
                and sha256(abort_target) == fixture["abortFile"]["sha256"],
                "续传后的文件字节与源一致", str(abort_target))
    ok &= check((pack / "config" / "e2e-dl.toml").is_file(),
                "包内配置照常落地")
    return ok


def verify_pack_disk(pack: Path) -> bool:
    """基本流程的落盘结果（界面说"更新完成"不算，磁盘为准）。"""
    ok = True

    def read(rel: str) -> str:
        return (pack / rel).read_text(encoding="utf-8").replace("\r\n", "\n")

    ok &= check(read("mods/old.jar") == "NEW", "mods 已替换")
    ok &= check(read("mods/added.jar") == "ADDED", "新增文件已落地")
    ok &= check(not (pack / "mods/gone.jar").exists(), "本地多余文件已删除")
    ok &= check(read("config/mine.toml") == "mine = 1\n", "玩家自己的配置没被动")
    ok &= check(read("config/sodium-options.json") == '{"render_distance": 8}\n',
                "智能剔除保护生效")
    ok &= check(read("resourcepacks/keep.zip") == "PK-KEEP", "无需更新的文件没被重写")
    return ok


def copy_shot(work: Path, target: Path) -> None:
    for name in ("e2e.png", "download.png"):
        shot = work / name
        if shot.is_file():
            target.write_bytes(shot.read_bytes())
            log(f"截图：{target}")
            return
    fails = sorted(work.glob("fail-*.png"))
    if fails:
        target.write_bytes(fails[0].read_bytes())
        log(f"失败截图：{target}")


def main() -> int:
    if not (WEB / "dist" / "index.html").is_file():
        print("缺少 web/dist：先 npm ci && npm run build", file=sys.stderr)
        return 1
    if not (WEB / "node_modules" / "playwright").is_dir():
        print("缺少 web/node_modules/playwright：先 npm ci", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pe-ui-e2e-"))
    home = tmp / "home"
    home.mkdir()
    env = child_env(home)
    procs: list[subprocess.Popen] = []
    ok = True

    try:
        # ---------------- 1. 基本流程 ----------------
        main_dir = tmp / "main"
        fixture = run_fixture(DEV / "make_fixture.py", main_dir, env)
        port = free_port()
        procs.append(start(
            [sys.executable, str(DEV / "bridge_server.py"),
             "--port", str(port), "--dist", str(WEB / "dist"),
             "--db", fixture["db"]], env, "bridge_server（基本流程）"))
        if not wait_port(port):
            log("bridge server 没起来")
            return 1
        code = run_node(port, main_dir, "basic")
        ok &= code == 0
        if code != 0:
            log(f"基本流程失败（退出码 {code}）")
        artifacts = {}
        art = main_dir / "artifacts.json"
        if art.is_file():
            artifacts = json.loads(art.read_text(encoding="utf-8"))
        if artifacts.get("exported"):
            ok &= verify_exported(Path(artifacts["exported"]))
        else:
            ok &= check(False, "拿到开发者导出的路径（artifacts.json）")
        copy_shot(main_dir, ROOT / "e2e-ui.png")

        # ---------------- 2. 真下载 ----------------
        dl_dir = tmp / "download"
        file_port = free_port()
        dl = run_fixture(DEV / "make_download_fixture.py", dl_dir, env,
                         "--port", str(file_port))
        procs.append(start(
            [sys.executable, str(DEV / "file_server.py"),
             "--dir", dl["serverDir"], "--port", str(file_port),
             "--delay-ms", os.environ.get("PULSES_E2E_DELAY_MS", "60")],
            env, "file_server（真下载）"))
        port2 = free_port()
        procs.append(start(
            [sys.executable, str(DEV / "bridge_server.py"),
             "--port", str(port2), "--dist", str(WEB / "dist"),
             "--db", dl["db"]], env, "bridge_server（下载）"))
        if not (wait_port(port2) and wait_port(file_port)):
            log("下载套件的服务没起来")
            return 1
        code2 = run_node(port2, dl_dir, "download")
        ok &= code2 == 0
        if code2 != 0:
            log(f"下载套件失败（退出码 {code2}）")
        ok &= verify_downloaded(dl)
        if code2 != 0:
            copy_shot(dl_dir, ROOT / "e2e-ui.png")

        return 0 if ok else 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
